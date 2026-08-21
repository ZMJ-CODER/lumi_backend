# Lumi RAG 与分域检索设计

> 版本：v2.0 ｜ 更新：2026-08-21
> 对应代码：`app/services/rag/`、`app/services/memory/`、`app/services/office_docs.py`、`app/services/orchestrator.py`

## 1. 目标与边界

项目复用 PostgreSQL、pgvector 与本地 embedding 服务，但不将所有用户相关文本混成一个语料库。系统按来源和授权边界分为长期知识、办公附件、长期记忆与公共知识四个 scope；前三者均归属于当前用户，公共知识必须显式调用。

核心原则：

1. **存储一体，策略分治**：共用向量基础设施，不共用检索策略或可见范围。
2. **先路由，后检索**：普通聊天一次默认只读一个主 scope，禁止静默三库混搜。
3. **办公不预检索**：办公任务进入 DAG 前不扫描任何资料；Planner 生成 `retrieval` / `office_doc` 节点后才按节点授权访问。
4. **低相关宁缺毋滥**：纯向量结果过阈值才注入；文档精确关键词命中可作为强证据保留。
5. **引用必须可验证**：只保存解析器实际产出的定位信息，不能伪造 PDF 页码或表格行号。

## 2. 四个检索 Scope

| Scope | 内容与存储 | 生命周期 | 默认检索策略 | 入口 |
| --- | --- | --- | --- | --- |
| `personal_knowledge` | 用户主动上传的 `documents` / `document_chunks` | 长期保留 | dense 向量 + 关键词 + RRF + 时效重排 | 普通聊天的明确资料引用、`query_knowledge` |
| `office_attachment` | `office_sessions`，大文件另建临时 `knowledge_spaces` 索引 | 会话 TTL，默认 24 小时 | 小文件全文注入；大文件只检索本会话临时空间 | 办公 DAG 的显式文档/检索节点 |
| `memory` | `memories`、`memory_profile` | 长期、衰减与用户删除 | 高阈值 dense 向量；关键词兜底默认关闭 | 普通聊天的明确历史/偏好引用 |
| `public_knowledge` | `is_public=true` 的 `knowledge_spaces` | 管理员维护 | 独立公共检索 API / 显式工作流 | `/public-kb/search` |

长期记忆不是上传文档 RAG 的替代品：它保存的是短事实、偏好、经历与目标，包含置信度、隐私等级、失效时间和覆盖关系。办公附件也不会自动晋升为长期知识库，用户必须主动上传或确认转存。

## 3. 路由与调用约束

普通聊天的路由规则在 `app/services/rag/scope.py`，完全基于规则，不新增一次 LLM 分类调用：

```text
显式 retrieval_query / 附件 / "文档里、资料里、知识库"
  -> personal_knowledge
明确 "上次、之前、还记得、按我的偏好"
  -> memory
普通问答、创作、闲聊
  -> none
```

资料引用优先于历史引用。例如“按我上次的格式总结这份文档”默认进入 `personal_knowledge`，防止旧记忆篡改文档事实。需要“读取合同后按用户偏好排版”一类跨 scope 任务时，必须由办公 DAG 以多个节点显式读取，不能在聊天预处理阶段拼接多个语料库。

公共知识默认不混入个人资料检索。返回的 citation 会通过 `type=personal/public` 标识来源，前端应展示来源库而非把公共语料误称为用户资料。

## 4. 长期知识库入库

```text
POST /knowledge/documents
  -> 文件落盘 data/uploads/{user_id}/{document_id}{ext}
  -> SHA-256 + space_id 去重，documents.status=pending
  -> Celery process_document
  -> 解析 -> 清洗 -> 质量门 -> 分类 -> 类型分块 -> bge-m3 embedding
  -> document_chunks 写入 pgvector，documents.status=ready
```

- 单文件上限 20 MB；同空间的 `pending`、`processing`、`ready` 重复文件不重复投递。
- 解析失败或质量门失败将文档标为 `error`，并记录可展示的错误原因；不写入向量。
- 纯文本、CSV、JSON、邮件、日历由内置解析器处理；PDF、Office、图片等走 Docling/OCR 路线。
- 非代码文档由分类器生成 `category` 和 `tags`；源码直接归为 `code`，默认不进入聊天和办公资料检索。

### 4.1 数据模型

| 表 | 作用 |
| --- | --- |
| `knowledge_spaces` | 用户空间、场景标签和公共空间标记 |
| `documents` | 原文件元数据、哈希、状态、分类、标签 |
| `document_chunks` | chunk 正文、1024 维向量、来源和定位 metadata |
| `office_sessions` | 聊天框办公附件的原文件、全文、TTL 和会话边界 |
| `memories` / `memory_profile` | 长期事实向量与常驻画像，不与文档表混用 |

`document_chunks.embedding` 为 `vector(1024)`，采用 pgvector `ivfflat` 余弦索引。`metadata` 为 JSON 文本，当前写入：`source`、`file_extension`、`chunk_index`、可用的 `heading_path` 与 CSV/TSV 的 `table_rows_in_chunk`。已有历史 chunk 无 metadata 时仍可检索，只是 citation 不带定位信息；重处理文档即可补齐。

### 4.2 清洗与质量门

清洗器去除不可见字符、重复空白、乱码与明显重复结构。质量评分低于 `RAG_MIN_QUALITY_SCORE`（默认 `0.5`）或命中不可解析、恶意载荷、严重损坏、极端噪声等硬拦截条件时，处理终止，文档不进入召回库。

### 4.3 类型感知分块

| 类型 | 当前策略 |
| --- | --- |
| 代码 | 按顶层语句和缩进边界分块，保留少量行重叠 |
| JSON/YAML/XML | 按顶层结构边界分块 |
| CSV/TSV | 表头在每块重复，按行组切分 |
| Markdown/Docling 文本 | 识别标题、表格、代码块与段落；标题作为上下文前缀 |
| txt/log | 递归字符切分，默认 500 字符、50 重叠 |

办公附件建立临时索引时，CSV/TSV 保留原扩展名以走表格分块；其它已提取的 Office/PDF 文本以 Markdown 方式索引，获取结构化分块效果。临时索引的文件名仅是内部实现，citation 始终显示用户上传的原始文件名。

### 4.4 嵌入与分类

- 嵌入模型：本地 `BAAI/bge-m3`，1024 维，`sentence-transformers` 懒加载单例。
- 文档与查询均 L2 归一化；查询可配置 bge 检索指令前缀。
- 模型优先从本地 cache 加载，缺失才下载；CUDA 不可用会自动降级 CPU。
- 分类是异步入库阶段工作，不阻塞上传 API；分类失败会降级，而不是让文档不可用。

当前适配器只输出 dense 向量。BGE-M3 sparse/ColBERT 不是配置开关，接入需要更换推理接口、增加存储列和实现新的召回 SQL；在有评测收益前不引入。

## 5. 检索链路

### 5.1 查询重写

优先级：

```text
前端明确 retrieval_query > 服务端 rewrite > 用户原问题
```

服务端重写默认只用于办公场景，通过 `RAG_QUERY_REWRITE_*` 配置选择云端文本模型或本地文本模型。视觉模型（如 `qwen2.5vl`）会被拒绝用于纯文本重写；重写失败直接回退原问题，不阻断回答。

### 5.2 长期知识混合召回

```text
query
  -> bge-m3 query embedding -> pgvector cosine Top 10
  + 关键词提取 -> ILIKE Top 10
  -> RRF (k=60) 融合 -> 时效重排 -> 相似度门槛
  -> Top K context + citations
```

- 默认最终 `Top K=5`，向量和关键词候选各为 10。
- 纯向量候选低于 `RAG_SIMILARITY_THRESHOLD` 会被丢弃；关键词精确命中是强证据，即使向量分低也可保留。
- 时效重排默认权重 `0.3`；问题含“最新、最近、今年”等时间意图时为 `0.6`。默认半衰期为 90 天，可按文档 category 覆盖。
- 当前关键词召回为 `ILIKE`，适合文件名、编号和工号等精确字符串，但规模扩大后会成为瓶颈。先由评测集确认瓶颈，再选择 PostgreSQL FTS/BM25 或 BGE-M3 sparse 方案。

### 5.3 办公附件检索

办公附件只有在 `office_doc` 或 `retrieval` 节点明确调用时访问：

- 全文不超过 `OFFICE_DOC_FULL_TEXT_LIMIT` 时直接作为该节点上下文，不经过阈值过滤。
- 超过阈值时，按附件专属 `scene_tag=officedoc_{doc_id}` 建临时知识空间，只从该空间取候选，禁止扫用户长期资料。
- 附件使用、分析或编辑时刷新 TTL；丢弃或过期时，DB 记录、磁盘缓存和临时 RAG 空间一起清理。

### 5.4 长期记忆检索

记忆是短事实而非长文档，默认只在显式历史引用时召回：快速档 Top 3，思考档 Top 5。纯向量结果必须达到 `MEMORY_FACT_MIN_VECTOR_SIMILARITY=0.75` 才能注入；关键词 fallback 默认关闭，只有评测证明其必要性后才可开启 `MEMORY_FACT_KEYWORD_FALLBACK_ENABLED=true`。

注入到模型的记忆带类型、来源时间和隐私处理标记。私密记忆先是占位符，只有用户明确请求且后端隐私门授权后才能解密。详情见 `MEMORY_DESIGN.md`。

### 5.5 上下文与引用

长期知识命中会以如下形式加入当前用户消息：

```text
[1] 合同.pdf | 付款条款
<chunk text>
```

`done` SSE 事件和办公节点结果均携带 citations。每条 citation 包含 `type`、`title`、`document_id`、`similarity`、`recency`、`score` 与 `locator`。`locator` 只使用实际入库的标题路径/块索引等信息；PDF 页码、表格原始行号和点击跳页属于下一阶段解析元数据增强，当前不可假定存在。

## 6. 权限、降级与安全

- 所有检索 SQL 强制按当前 `user_id` 限定；公共空间需显式满足 `is_public=true`。
- 个人空间默认不混入公共空间，`RAG_INCLUDE_PUBLIC_IN_PERSONAL=false`。
- 聊天与办公 RAG 默认排除 `category=code`；代码检索应走独立授权路径。
- embedding 加载、查询重写、向量检索或关键词检索失败时返回空上下文或可用单路结果，不阻塞正常 LLM 回答。
- 文档片段、网页、附件和检索结果均为不可信数据，不能提升权限、注入系统指令或授权工具调用。

## 7. 配置

| 配置 | 当前默认 | 说明 |
| --- | --- | --- |
| `EMBEDDING_MODEL` / `EMBEDDING_DIMENSION` | `BAAI/bge-m3` / `1024` | 本地 dense embedding |
| `RAG_TOP_K` | `5` | 最终资料引用数量 |
| `RAG_SIMILARITY_THRESHOLD` | `0.5` | 纯向量硬门槛，生产值由评测定 |
| `RAG_HYBRID_VECTOR_TOP_K` / `KEYWORD_TOP_K` | `10` / `10` | 长期知识候选数 |
| `RAG_CHUNK_SIZE` / `RAG_CHUNK_OVERLAP` | `500` / `50` | 纯文本默认切分参数 |
| `RAG_MIN_QUALITY_SCORE` | `0.5` | 入库质量门 |
| `RAG_RECENCY_WEIGHT` / `QUERY_WEIGHT` | `0.3` / `0.6` | 常规/时间意图时效权重 |
| `OFFICE_SESSION_TTL_HOURS` | `24` | 临时办公附件生命周期 |
| `OFFICE_DOC_FULL_TEXT_LIMIT` | `20000` | 小附件全文注入阈值，实际分析路径上限为 12000 字符 |
| `MEMORY_FACT_MIN_VECTOR_SIMILARITY` | `0.75` | 长期记忆注入门槛 |
| `MEMORY_FACT_KEYWORD_FALLBACK_ENABLED` | `false` | 记忆关键词兜底开关 |

`.env.example` 与代码默认值一致，但它不是质量调优的依据。部署配置应由评测数据确定，并通过 Redis 动态 RAG 配置的覆盖机制审慎灰度。

## 8. 评测与调参

阈值、分块器、reranker 或 embedding 变更前，必须先维护 50-100 条标注用例，包括应命中、必须零命中、文件名/编号、表格、歧义问题和记忆回忆。模板位于 `tests/fixtures/rag_eval_cases.json`。

```powershell
uv run python scripts/evaluate_rag.py --user-id <uuid> --cases <labelled-cases.json> --thresholds 0.5,0.55,0.6,0.65
```

脚本输出逐条命中与 aggregate precision/recall。选择能满足业务精度的最低延迟配置，而不是凭经验将阈值设为某个固定值。评测集必须使用隔离测试账号，不能将真实用户资料或私密记忆提交到仓库。

## 9. 接口与异步任务

| 类别 | 接口/任务 | 说明 |
| --- | --- | --- |
| 知识空间 | `POST/GET/PATCH/DELETE /knowledge/spaces` | 创建、管理、删除长期空间 |
| 文档 | `POST /knowledge/documents` | 批量上传，异步入库 |
| 文档 | `GET/DELETE /knowledge/documents` | 查询状态或删除文档及向量 |
| 公共库 | `POST /public-kb/search` | 显式公共知识检索 |
| 入库任务 | `celery_app.tasks.process_document` | 解析、清洗、分类、嵌入 |
| 清理任务 | 办公会话清理 | 删除过期附件及其临时 RAG 空间 |

## 10. 后续路线

1. 用真实标注集确定不同 scope 的阈值，并将评测纳入 CI。
2. 将 Docling 的页码、标题树、表格坐标写入 chunk metadata，前端支持 citation 定位跳转。
3. 在思考档和复杂办公检索中，对混合 Top20 做 cross-encoder rerank；快速档不增加这一步。
4. 基于压测结果替换大规模场景下的 `ILIKE`，保留文件名/编号精确匹配能力。
5. 在附件 TTL 到期前提供用户驱动的“转存长期知识库”操作，并按 SHA-256 复用可复用解析结果。
