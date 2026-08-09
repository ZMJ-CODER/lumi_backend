# Lumi RAG 知识库设计文档

> 版本：v1.0 ｜ 更新：2026-08-09
> 对应代码：`app/services/rag/`、`app/api/v1/knowledge.py`、`app/api/v1/public_kb.py`、`celery_app/tasks.py`

## 1. 概述

RAG（Retrieval-Augmented Generation）为对话智能体提供"基于自有知识库回答"的能力。
本设计围绕四个目标：

1. **多格式支持**：文本 / PDF / Office / 图片等文档均可入库
2. **质量可控**：清洗 + 质量门，防止"垃圾进垃圾出"
3. **召回准确**：混合检索 + 时效性加权，提升命中率
4. **可插拔**：嵌入模型、分块策略、查询重写均可替换扩展

## 2. 总体架构

```
┌─────────────────────────── 文档入库链路（异步） ───────────────────────────┐
│ 上传 → 落盘 → documents(pending) → Celery process_document                 │
│   → 解析(Docling/内置) → 清洗 → 质量门 → 分类 → 结构化分块 → 嵌入 → 入库(ready)│
└────────────────────────────────────────────────────────────────────────────┘
┌─────────────────────────── 对话检索链路（同步） ────────────────────────────┐
│ 用户提问 → 查询重写(可插拔) → 混合检索(向量+关键词+RRF) → 时效加权            │
│   → 上下文+引用 → LLM → 回答                                                 │
└────────────────────────────────────────────────────────────────────────────┘
```

## 3. 数据模型

### knowledge_spaces（知识空间）

| 字段 | 说明 |
|---|---|
| `id` | UUID 主键 |
| `user_id` | 归属用户 |
| `name` / `description` | 空间名称与描述 |
| `scene_tag` | 场景标签：chat / office / game（检索时按场景过滤） |
| `is_public` | 是否公共空间（仅管理员可设 true） |

### documents（文档）

| 字段 | 说明 |
|---|---|
| `id` / `space_id` / `user_id` | 主键与归属 |
| `filename` / `file_hash` / `file_size` | 文件信息（sha256 去重） |
| `status` | pending / ready / error |
| `chunk_count` | 分块数 |
| `category` | 时效档次：news / general / history / other |
| `tags` | 开放主题标签（逗号分隔） |
| `created_at` / `updated_at` | 创建 / 更新时间（更新触发重处理时刷新） |

### document_chunks（分块 + 向量）

| 字段 | 说明 |
|---|---|
| `document_id` / `space_id` / `user_id` | 归属 |
| `chunk_index` / `chunk_text` | 分块序号与文本 |
| `embedding` | `vector(512)`，ivfflat 余弦索引 |
| `metadata` | 预留（结构化分块元数据，如标题路径） |

## 4. 文档入库管线

### 4.1 解析（`document_parser.py` + `docling_parser.py`）

- 18 种纯文本格式（txt/md/json/csv/代码等）：内置 UTF-8 / GB18030 解码
- 其余格式（PDF/Word/PPT/Excel/图片）：Docling 解析为 Markdown，
  支持布局分析、表格结构、OCR（RapidOCR，配置 `DOCLING_ENABLE_OCR`）
- 解析失败归类为 `unparsable`，终止处理不重试

### 4.2 清洗（`cleaner.py`）

字符级：去控制符 / 零宽字符 / 替换符；全角→半角；mojibake 乱码尽力修复。
空白级：压缩连续空行、去行尾空格。
结构级：去重复页眉页脚；代码单行恢复。

### 4.3 质量门（`cleaner.assess_document`）

输出 0~1 质量分 + 分类问题清单，8 类：

| code | 检测 | 是否硬拦截 |
|---|---|---|
| unparsable | 解析失败 / 内容为空 | ✅ |
| encoding_errors | mojibake 标记 > 5% | 计分 |
| corrupted_data | 控制符/乱码 > 20% | ✅ |
| incomplete_parsing | 文件 >50KB 但文本 <200 字 | 计分 |
| security_vulnerabilities | 代码命中危险模式（eval/exec/shell 注入等） | ✅ |
| malicious_content | 恶意载荷模式（powershell -enc/certutil 等） | ✅ |
| extreme_noise | 可读字符占比 < 阈值（代码 0.2 / 其他 0.5） | ✅ |
| unintelligible_text | 单字符重复占比 > 60% | ✅ |

低于 `RAG_MIN_QUALITY_SCORE`（0.5）或命中硬拦截 → `status=error` 并记录原因。
同时前端待传区持久化提示上传失败。

### 4.4 分类（`classifier.py`）

**时效档次 + 开放标签**双字段，大模型抽样判断（≤6000 字符，token 成本与文档大小无关）：

- `category`：news / general / history / other（固定，决定半衰期）
- `tags`：1~3 个开放主题标签（展示/过滤用）

兜底链：LLM 判断 → 用户上传选择的档次 → 本地嵌入零样本 → 默认档次。

### 4.5 结构化分块（`chunker.py`）

注册表按扩展名路由分块策略：

| 类型 | 策略 |
|---|---|
| 代码（py/js/ts/c/java…） | 行级分块，语句/缩进边界，2 行重叠 |
| 配置（json/yaml/xml） | 按顶层结构边界切 |
| 表格（csv/tsv） | 表头 + 每 50 行一组 |
| Markdown / Docling 输出 | 识别代码块/表格/标题/段落，按块选策略，标题作为前缀 |
| 纯文本（txt/log） | 递归字符切分（500/50） |

### 4.6 嵌入（`embeddings.py`）

- 本地模型 `BAAI/bge-small-zh-v1.5`（512 维），sentence-transformers 推理
- L2 归一化（余弦相似度 = 1 - 余弦距离）
- 懒加载单例 + 本地优先加载（离线可用），HF 国内镜像兜底
- 查询向量可选检索指令前缀（实测默认关闭区分度更好）

### 4.7 入库

分块文本 + 向量写入 `document_chunks`，`documents.status=ready`。
失败（质量门/异常）→ `status=error`，质量类问题不重试。

## 5. 检索链路

### 5.1 查询重写（可插拔槽位，`query_rewriter.py`）

```
优先级：客户端 retrieval_query（本地小模型精炼）> 服务端重写 > 原文
```

- 客户端：Electron 可插拔本地模型，配置后自动精炼提问
- 服务端：`RAG_QUERY_REWRITE_*` 配置（默认关闭），手机端等无本地模型场景兜底

### 5.2 混合检索（`knowledge.py`）

双路召回 + RRF 融合：

```
第一路：向量相似度 top-10（pgvector <=> 余弦距离，不做阈值过滤）
第二路：jieba 关键词 top-10（chunk_text ILIKE 命中数）
→ Reciprocal Rank Fusion（k=60）合并 → 取 top_k
```

关键词路能召回向量 top-10 之外但精确命中术语的片段；
去阈值使相似度略低于 0.45 的弱相关片段也能进入候选（基准证实这是主要召回增益来源）。

### 5.3 时效性加权

```
final = (1 - w) × 相关性归一 + w × recency
recency = exp(-age / 文档时效档次的半衰期)
```

| 时效档次 | 半衰期 |
|---|---|
| news | 14 天 |
| general | 180 天 |
| history | 3650 天（10 年） |
| other | 365 天 |

查询含时间意图（"最新/最近/今年"等）时权重 0.6，否则 0.3。
可选硬过滤 `RAG_TIME_FILTER_DAYS`（默认不过滤）。

### 5.4 上下文与引用

命中的 chunk 按 `[序号] 内容` 拼接进 System/上下文，返回引用列表
（type/title/content/source/document_id/similarity/score/recency/category）。

## 6. 接口

### 知识空间

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/knowledge/spaces` | 创建空间（管理员可设 is_public） |
| GET | `/knowledge/spaces` | 我的空间（含公共） |
| PATCH | `/knowledge/spaces/{id}` | 更新（公共标记仅管理员） |
| DELETE | `/knowledge/spaces/{id}` | 删除（级联清向量） |

### 文档

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/knowledge/documents` | 批量上传（files 多文件 + category 时效档次参考） |
| GET | `/knowledge/documents?space_id=` | 文档列表（含 category/tags/updated_at） |
| DELETE | `/knowledge/documents/{id}` | 删除文档及向量 |

### 公共检索

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/public-kb/search` | 公共库检索；带 query 文本走混合检索，否则纯向量 |

## 7. 配置项

| 配置 | 默认 | 说明 |
|---|---|---|
| `RAG_TOP_K` | 5 | 最终返回条数 |
| `RAG_SIMILARITY_THRESHOLD` | 0.45 | 纯向量路径阈值（混合路径仅作参考） |
| `RAG_CHUNK_SIZE` / `RAG_CHUNK_OVERLAP` | 500 / 50 | 文本分块参数 |
| `RAG_MIN_QUALITY_SCORE` | 0.5 | 质量门阈值 |
| `RAG_HYBRID_VECTOR_TOP_K` / `KEYWORD_TOP_K` | 10 / 10 | 双路召回数 |
| `RAG_RECENCY_WEIGHT` / `QUERY_WEIGHT` | 0.3 / 0.6 | 时效权重 |
| `RAG_RECENCY_HALF_LIFE_DAYS` | 90 | 默认半衰期 |
| `RAG_CATEGORY_HALF_LIFE_DAYS` | 见上表 | 按档次半衰期 |
| `RAG_QUERY_REWRITE_*` | 关闭 | 服务端查询重写 |
| `EMBEDDING_MODEL` / `DIMENSION` | bge-small-zh-v1.5 / 512 | 嵌入模型 |
| `DOCLING_ENABLE_OCR` | true | Docling OCR（RapidOCR） |

## 8. 异步任务（Celery）

| 任务 | 说明 |
|---|---|
| `process_document` | 入库管线；质量类失败不重试 |
| `rebuild_index` | 重建 ivfflat 向量索引 |
| `cleanup_vectors` | 清理孤儿向量 |
| `delete_user_data` | 物理清理用户数据 |

## 9. 基准测试（2026-08-09）

语料 10 篇 / 查询 20 条（精确术语、同义改写、模糊、标识符、表格、代码），top-5：

| 指标 | 纯向量 | 混合检索 |
|---|---|---|
| recall@1 | 90% | 90% |
| recall@3 | 95% | 100% |
| recall@5 | 95% | 100% |
| 漏检 | 1 | 0 |

主要结论：
- 混合检索的召回增益主要来自"取消向量阈值"（模糊查询弱相似片段得以进入候选）
- 真正的难点是**无实体重叠的模糊查询**（如"服务器经常挂怎么办"）——
  本地小模型查询重写是首要优化方向
- 基准脚本：`scripts/benchmark_rag.py`，可重复执行对比

## 10. 关键决策与权衡

1. **本地嵌入而非 API**：bge-small-zh-v1.5 本地推理，隐私与成本可控，512 维与 pgvector 匹配
2. **Docling 负责解析、内置负责文本**：文档格式多样性由 Docling 承接，纯文本走轻量内置路径
3. **时效档次 + 开放标签分离**：半衰期只与"知识多变程度"有关，主题多样性交给标签
4. **质量门宁可误拦**：垃圾不入库优于漏掉，误拦可在管理后台看到原因并重传
5. **启发式规则为主**：清洗/分块/安全检测均为可扩展规则表，不为完美解析引入重型依赖

## 11. 可扩展点

| 方向 | 位置 | 说明 |
|---|---|---|
| 图片 RAG | 管线"解析"环节 | VLM 生成描述 → 复用现有入库/检索 |
| 多模态嵌入 | `embeddings.py` | 引入 CLIP 类模型做图像/文本联合检索 |
| 重排（rerank） | 融合后 | 引入 LLM/交叉编码器重排 top-k |
| 检索缓存 | 检索前 | Redis 缓存向量与结果，支撑高并发 |
| 查询重写 | `query_rewriter.py` | 已预留客户端/服务端双槽位 |
| 新格式分块 | `chunker.py` 注册表 | 注册一个函数即接入 |
| 分类标签扩展 | `classifier.py` | 增删类别与半衰期映射 |

## 12. 已知局限与后续

- 照片/无文字图片无法检索（需多模态嵌入 + VLM 描述，见扩展点）
- 模糊口语查询召回仍依赖查询重写（本地小模型未接入时效果有限）
- 混合路径无阈值，弱相关片段可能占位（可加"软下限"）
- 长文档大表格分块后检索上下文截断（chunk 级元数据/标题路径可进一步优化）
