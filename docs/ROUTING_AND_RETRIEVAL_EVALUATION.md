# 路由与检索评测方法

本文记录 Lumi 中三类容易被混淆的指标，以及它们的可复现实验边界：聊天工具候选/参数、四级执行路由和公共 RAG 检索。所有命令均不会执行真实工具；是否需要模型或 GPU 会在相应章节说明。

## 1. 先区分三个问题

| 问题 | 被测对象 | 数据来源 | 可以得到的结论 | 不能得到的结论 |
| --- | --- | --- | --- | --- |
| 工具选择与参数 | 聊天场景的首轮 Function Calling | 60 条人工标注 query → tool → params | 候选召回、选工具、严格参数、误调用、工具目录 token 开销 | 四级执行路径的真实流量占比 |
| 四级路由 | `direct_llm` / `deterministic_script` / `rag` / `agent` 的确定性分类 | 80 条通道路由标注 + 生产匿名遥测 | 离线路由正确率；生产路由占比 | LLM 工具选择准确率或实际执行成功率 |
| 公共 RAG | Dense 与词法代理 + RRF 的排序 | BEIR SciFact qrels | 公共英文语料的 Recall@10 / nDCG@10 趋势 | 生产 PostgreSQL `ILIKE` 的线上增益 |

不要将这三种评测的分母、数据集或成本混为同一个“准确率”。

## 2. 四级路由分流比例

### 2.1 离线回归基准

标注集在 `tests/fixtures/four_channel_routing_eval.jsonl`。它包含 80 条代表性原子任务，每个通道 20 条，刻意均衡用于覆盖边界：

- `direct_llm`：改写、解释、创作等无外部状态任务；
- `deterministic_script`：有明确文件格式转换/批处理目标；
- `rag`：已授权资料、知识库或多文档事实定位；
- `agent`：多步骤、外部系统操作、状态核验或写入。

运行：

```powershell
.venv\Scripts\python.exe scripts\evaluate_four_channel_routing.py `
  --output artifacts\four-channel-routing-eval
```

输出 `report.json` 和 `report.md`，包括 exact-route accuracy、混淆矩阵、路由原因和**预估** token 分布。该脚本只调用 `route_atomic_instruction()`，不调用 LLM、不查询资料、不运行脚本、不创建 Agent 任务。

当前代码修正后的离线回归结果（2026-08-29）：80/80，exact-route accuracy 为 **100%**。修正前同一标注集为 71/80（88.75%），暴露的真实边界包括：

1. 文件名出现在“转为/导出”前时，文件转换没有被识别；
2. “知识库中的审批节点”将名词“审批”误解为执行审批；
3. “改成正式通知语气”被误解为发送通知。

相应修复限定在路由模式与特征构造：支持文件名前置、将只读“审批节点”与状态核验分开、移除把“通知”单独当作反馈修复动作的规则；均有回归测试。这不是通过调整标签得到的分数。

**重要：**该基准的路由比例固定为 25% / 25% / 25% / 25%，仅说明评测集均衡，不能当作生产分流比例。

### 2.2 真实任务遥测与比例

每次创建清单 manifest 时，后端对每个原子任务记录一条不含用户原文和参数的事件：

```text
FOUR_CHANNEL_ROUTE_DECISION {"route":"rag","reason":"...","estimated_tokens":1200,...}
```

日志字段仅有 route、reason、estimated_tokens 和清单内局部 item id。实际运行足够有代表性的任务后，聚合真实日志：

事件仅在任务通过准入并成为可执行 Job 后写入；因用户/全局容量被拒绝的提交不会产生该事件，避免将
重试或 429 请求计入路由比例。

```powershell
.venv\Scripts\python.exe scripts\aggregate_four_channel_routing_logs.py `
  logs\lumi_2026-08-29.log `
  --minimum-events 100 `
  --output artifacts\four-channel-routing-telemetry\2026-08-29.json
```

聚合结果中的 `sample_sufficient` 仅在事件数达到 `minimum_events`（默认 100）时为 `true`。至少应记录时间范围、事件数、是否排除了测试/重试流量，以及四个 `route_shares`。推荐选择一个只包含真实任务的连续观察窗口，再将结果定义为该观察期内的生产路由比例。示例数字仅用于说明计算方式，不属于当前实测结果。

当前状态：**尚未采集生产路由遥测数据**。只有部署包含 `FOUR_CHANNEL_ROUTE_DECISION` 事件的版本并完成至少 100 个真实原子任务后，才能填入实际比例。建议采集开始和结束后分别导出对应时间窗口的 API 容器日志，避免与本地测试或历史任务混合：

### 2.3 评测边界

曾用于验证路由遥测的真实 LLM dry-run 提交入口已从生产 API 和编排器移除。生产提交始终走正常派发链路，不存在通过请求头或环境变量把 Job 强制取消、跳过工具执行的分支。需要无副作用的路由评测时使用第 3 节的离线脚本和 `EvaluationTool` 测试替身；它们不注册到生产 SkillRegistry，也不参与正常请求。

仓库中的合成清单和提交脚本只用于历史链路验证，不得用于生产比例统计。

#### 合成清单遥测链路实测（2026-08-29）

在本机 API（`http://127.0.0.1:8001`）上提交上述五份清单，并在每次成功创建后立即调用
取消接口。为避免某个已取消测试任务的历史准入状态影响补跑，缺失的清单使用了不同的隔离
`loadtest-*` 账号；账号不参与路由判断，事件只记录清单中的原子任务路由。聚合窗口为
`artifacts/four-channel-routing-telemetry/clean-window.log`，原始报告为
`artifacts/four-channel-routing-telemetry/report.json`。

| 字段 | 实测值 |
| --- | ---: |
| 路由事件数 | 100 |
| 最小样本门槛 | 100 |
| `sample_sufficient` | `true` |
| 畸形事件数 | 0 |
| `direct_llm` | 25（25.00%） |
| `deterministic_script` | 25（25.00%） |
| `rag` | 25（25.00%） |
| `agent` | 25（25.00%） |

`estimated_token_counts` 分别为 20,211、40,435、30,234、87,813；它们是路由器的预算估计，
并非模型账单。四个通道各 25% 是清单刻意均衡的设计结果。该实测只能证明：已准入的 manifest
会逐项写出路由事件，聚合器可完整读取四个通道且不含畸形事件；它不描述真实用户任务的路由比例。

### 历史：单次 100 项受控策略回放（2026-08-30，已移除入口）

为避免五次提交受到单用户提交令牌桶和活动任务额度的干扰，将
`tests/fixtures/four_channel_live_llm_replay.jsonl` 的五组各 20 项输入合成为一份显式编号清单，
并以 `X-Lumi-Evaluation-Dry-Run: true` 提交一次。本机评测进程临时设置
`AGENT_MANIFEST_TOKEN_BUDGET=200000`，只用于允许该 100 项安全 dry-run 通过预算检查；生产默认
预算没有改动。显式编号清单使用确定性解析以保留全部事项，因此本次测量的是原子事项的实际路由策略，
不是 100 项自然语言清单抽取准确率。真实 LLM 清单解析和不派发执行的安全合同已由此前单项试点单独验证。

原始提交记录为
`artifacts/four-channel-routing-live-replay/combined-100-20260830-001.jsonl`，其中 Job 为
`e2ece427-8e9e-44b5-8bc4-4da79cebffb2`，状态 `cancelled`、`dry_run=true`、
`execution_dispatched=false`。任务仅物化首批 10 个运行节点；路由事件在取消前对完整 manifest
的 100 个事项各记录一次。日志按提交秒级窗口提取到
`artifacts/four-channel-routing-live-replay/combined-100-window.log`，再使用
历史版本曾用 `--only-evaluation-dry-run --minimum-events 100` 聚合为
`artifacts/four-channel-routing-live-replay/combined-100-report.json`。

| 字段 | 实测值 |
| --- | ---: |
| 路由事件数 | 100 |
| `sample_sufficient` | `true` |
| 畸形事件数 | 0 |
| `direct_llm` | 71（71.00%） |
| `deterministic_script` | 6（6.00%） |
| `rag` | 13（13.00%） |
| `agent` | 10（10.00%） |
| 路由预算估计总量 | 117,701 |

输入文本按四通道各 25 项构造，但当前策略实际输出并不均衡：71 项被归入 `direct_llm`。
这是当前路由规则对该批输入的真实判定结果，应作为后续检查意图短语、描述与路由策略覆盖的依据；
它不代表生产用户流量比例，也不能视作供应商 API 账单。

后续审查发现上述偏差来自内置正则对文件批处理、资料检索和多步协同同义表达的漏召回。为避免
“修改关键词即修改路由器”的耦合，意图短语已迁移到
`config/agent_policies/route_intent_patterns.yaml`：该文件只允许受限的文件动作/目标、检索动作/来源及
协同短语；它不能定义通道、工具、权限、正则或可执行表达式。源码固定保留特征计算、通道优先级和
安全边界，`routing_rules.yaml` 则只基于特征选择通道。100 项 fixture 已加入回归契约；调整短语必须先
更新该契约或新增独立样本，不能仅依赖人工观察。

补充一轮常用中文表达后，新增短语仍只进入外置词典，不为单个句子增加源码分支。扩展覆盖
“清理/去除/过滤/查重/抽取/解析/汇总/规范化/预览”等文件动作，“参考/查阅/阅读/浏览”及
Wiki、FAQ、制度、标准、设计文档等资料来源，以及“编排/调度/诊断/排查/分析后/根据结果”等
协同信号。过宽的“评估”词条在回归中造成普通知识检索误判为 Agent，已移除。扩展后 100 项回归
仍保持 `direct_llm=25`、`deterministic_script=25`、`rag=25`、`agent=25`，相关测试共 77 项通过。

```powershell
# 记录开始时间后，完成真实任务观察窗口；结束时导出该时间段 API 日志。
docker compose logs --no-color --since "2026-08-29T09:00:00" --until "2026-08-29T18:00:00" api `
  | Set-Content -Encoding utf8 artifacts\four-channel-routing-telemetry\api-window.log

.venv\Scripts\python.exe scripts\aggregate_four_channel_routing_logs.py `
  artifacts\four-channel-routing-telemetry\api-window.log `
  --minimum-events 100 `
  --output artifacts\four-channel-routing-telemetry\report.json
```

`estimated_token_shares` 是路由器预算，不能替代供应商账单。四级路由的真实成本对照需将同一安全回放集分别强制 Agent 与按路由执行，按 run id 汇总 `LLMUsage` 的 prompt/completion token，再按当期价格表换算。

## 3. 严格工具参数准确率

工具评测不执行真实插件：`EvaluationTool` 复制生产能力的工具名、描述和 JSON Schema，但执行只返回固定 synthetic result。模型只进行第一轮 Function Calling 后立即停止。因此桌面打开、联网、文件、邮件和知识库不会执行。

参数评分是 BFCL 风格 JSON AST 结构比较：对象键顺序无关，字段名、数组顺序和值必须一致；不会通过语义相似度把改写后的搜索词判为正确。每次失败还按以下原因聚合：

- `missing_expected_field`：漏掉必填/默认字段；
- `unexpected_field`：多传字段；
- `string_whitespace_changed`：字符串仅空白变化；
- `string_value_changed`：文本实体、限定词或表达式改变；
- `non_string_value_changed`：枚举、数值等非文本值错误；
- `malformed_arguments`：不是 JSON 对象。

参数契约优化内容：日期工具要求显式 `format`；网页检索要求 `max_results`；知识库检索要求 `top_k`；参数描述和模型选择合同要求保留原始表达式、实体及时间/地域限定，且未指定条数时明确填写默认值。这些规则改变生产 Schema，而不是评测评分规则。

用同一个模型、同一 60 条集、同一重复次数同时重跑 A/B：

```powershell
.venv\Scripts\python.exe scripts\evaluate_tool_routing.py `
  --mode both --live --repeat 3 `
  --output artifacts\tool-routing-eval-param-contract
```

报告前检查两组 `valid_decision_count == case_count`、`error_rate == 0`，再比较 `parameter_accuracy_given_correct_tool` 和 `parameter_mismatch_reasons`。如果严格参数分数提升而工具选择、误调用或 API 错误变差，则不能称为有效优化。

此前参数契约优化前的 DeepSeek 基线为：Routed 的参数严格准确率 54.17%，Baseline 为 53.33%；该基线用于比较，不是本轮优化后的结果。

### 3.1 参数契约优化后复测（2026-08-29，DeepSeek）

按上述命令运行 Routed 模式、60 条评测集重复 3 次，共 180 次首轮选择；
`valid_decision_count=180`、`error_rate=0`，测试替身边界保持不执行真实工具。

| 指标 | 优化前 Routed | 优化后 Routed | 变化 |
| --- | ---: | ---: | ---: |
| 候选召回率 | 100.00% | 100.00% | 持平 |
| 工具选择准确率 | 100.00% | 99.44% | -0.56pp |
| 严格参数准确率（选对工具后） | 54.17% | **71.43%** | **+17.26pp** |
| 完全正确率 | 69.44% | **80.56%** | **+11.12pp** |
| 误调用率 | 0.00% | 0.00% | 持平 |
| 总 Token | 117,102 | 128,764 | +9.96% |

两次都是同一 fixture、模型和重复次数，但属于独立模型运行，token 和极少量工具选择差异会受
供应商采样/服务端版本波动影响。比较两轮变化时应同时保留两轮原始 JSON；若需要严格 A/B
成本结论，应在同一时间窗口再运行一次 `--mode both --live --repeat 3`。

优化后各工具的严格参数拆分：

| 工具 | 选对后样本 | 严格正确 | 准确率 | 主要剩余问题 |
| --- | ---: | ---: | ---: | --- |
| `calculator` | 24 | 24 | 100.00% | 无 |
| `get_datetime` | 24 | 18 | 75.00% | `format` 枚举值选择错误 |
| `open_app` | 23 | 23 | 100.00% | 无 |
| `query_knowledge` | 24 | 9 | 37.50% | query 改写；少量 `top_k` 值变化 |
| `web_search` | 24 | 11 | 45.83% | query 改写；少量空白变化 |

全局不匹配统计为 `string_value_changed=31`、`non_string_value_changed=2`、
`string_whitespace_changed=2`。这表明必填字段契约已显著减少“参数缺失”，后续主要问题是
检索 query 被模型改写。由于该评测采用严格 AST 口径，不应把改写直接算作正确；若继续优化，
应为两个检索工具补充“原文 query 复制”的选择示例，或在产品契约上明确何时允许 query rewrite，
并据此单独建一个 rewrite 评测集，而不是放宽本评测的定义。

## 4. SciFact：Dense 与词法代理 RRF

`scripts/benchmark_public_rag.py` 使用 BEIR `GenericDataLoader` 加载 SciFact test 的 corpus、queries 和 qrels，以 `BAAI/bge-m3` 生成 Dense 向量。Dense 候选和词法通道候选各取 Top 10，使用 RRF（默认 `k=60`）融合。

指标：

- `Recall@10`：前 10 是否包含至少一个相关文档的 query 比例；
- `Hit@1` / `Hit@5`：前 1 / 前 5 包含相关文档的比例；
- `MRR`：第一个相关文档的倒数排名均值；
- `nDCG@10`：按 BEIR qrels 计算 `DCG@10 / IDCG@10`，增益为 `(2^rel - 1) / log2(rank + 1)`。它利用完整排序与 graded relevance，不等同于只看第一个命中。

已下载的 SciFact 目录可被脚本直接读取，不依赖 `beir` 包；只需要可用的
`sentence-transformers`、`torch` 与本地/可下载的 embedding 模型。若环境依赖完整，也可执行
`uv sync --extra rag-eval`，但 Windows 下文件被 Python、IDE 或终端占用而出现 `os error 5`
时，不必为了这次 SciFact 评测反复卸载环境，先关闭占用 `.venv` 的进程后再维护环境即可。
本机运行（不需要 Docker，不访问生产数据库）：

```powershell
uv sync --extra rag-eval
.venv\Scripts\python.exe scripts\benchmark_public_rag.py `
  --dataset scifact `
  --data-dir data\eval\scifact `
  --device cuda `
  --rrf-candidate-depth 10 `
  --rrf-k 60 `
  --skip-sparse `
  --output artifacts\rag-eval\scifact-dense-vs-lexical-proxy-rrf.json
```

CPU 环境将 `--device cuda` 改为 `--device cpu`；结果可复现但耗时会明显增加。命令输出还带有 `metric_protocol`、RRF 参数、模型名称和 `lexical_channel.production_equivalence=false`，方便审阅。

### 4.1 不能把它叫成生产 ILIKE

公共 SciFact 词法通道是英文空白分词后的词频重叠代理，**不是**生产 PostgreSQL 的 `ILIKE` / `pg_trgm` SQL 路径。因此该公共集结论的比较对象仅为“Dense vs Dense + lexical-proxy RRF”，不能推导为“Dense + 生产 ILIKE + RRF”的结果。

### 4.2 SciFact 复测结果（2026-08-29）

实际运行环境：本地已下载的 BEIR SciFact test（5,183 documents / 300 queries）、
`BAAI/bge-m3`、CUDA、每路 candidate depth=10、RRF `k=60`、未运行 sparse 通道。脚本通过
SciFact 的标准 `corpus.jsonl`、`queries.jsonl`、`qrels/test.tsv` 读取数据，并以 BEIR qrels
计算指标。

| 方案 | Hit@1 | Hit@5 | Recall@10 | MRR | nDCG@10 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Dense | **0.5100** | **0.7400** | **0.8000** | **0.6083** | **0.6437** |
| Dense + lexical-proxy RRF | 0.1900 | 0.6567 | 0.7533 | 0.4064 | 0.4834 |

相对 Dense，词法代理融合的 Recall@10 下降 **4.67pp**、MRR 下降 **20.19pp**、nDCG@10
下降 **16.03pp**。该结果不是“没有测到提升”，而是可复现地表明该公共英文语料与当前
词法代理/RRF 参数组合不适合启用融合；因此没有将该路线作为生产默认能力。

这依然不能外推成“生产 ILIKE 无效”：公共通道是空白分词词频代理，不是实际 PostgreSQL
`ILIKE/pg_trgm` SQL。若要评价真实 `ILIKE + RRF`，下一步应在隔离测试账号的真实 PostgreSQL
分块上人工标注 50–100 条中英文、编号、表格和术语 query，分别调用 Dense-only 与生产 hybrid
SQL 路，使用同一 qrels 计算 Recall@10 / nDCG@10。只有这个私有标注集才能支持“生产 ILIKE”收益
的结论。

## 5. 数据解释与复核要求

- 60 条 Function Calling 标注集的指标仅描述首轮工具选择和参数生成；全工具注入与候选工具注入的历史 A/B 总 token 差异为 64.55%，不代表四级执行路径成本。
- 80 条四级路由回归集覆盖 direct/script/RAG/Agent 的路由边界；100% 准确率仅适用于该固定离线标注集。
- 在 BEIR SciFact（5,183 文档 / 300 query）上，Dense + lexical-proxy RRF 相较 Dense 的 Recall@10 下降 4.67pp、nDCG@10 下降 16.03pp，因此当前不将该公共 proxy 融合作为默认检索路径。

生产路由比例必须来自有时间范围的真实遥测；生产 `ILIKE` 融合效果必须来自同构 SQL 路径与真实标注集。所有比较均应保留命令、commit、模型版本、数据集版本、样本量和原始 JSON 报告路径，以保证可复核。
