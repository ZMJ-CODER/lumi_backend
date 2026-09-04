# Tool / Skill 执行工作流

本文定义模型、DAG 与工具之间的边界。目标不是让模型“有工具就调用”，而是用最小能力、最小权限和可审计结果完成请求。

### 用户 Workflow Skill 接口

前端可使用 `GET/POST/PATCH/DELETE /api/v1/workflow-skills` 查看和管理工作流。`GET` 同时返回开发者公共 Skill 与当前用户的私有 Skill；创建、更新、删除只作用于当前用户的私有记录。创建体只能包含 `name`、说明、场景、`allowed_tools`、`steps` 与浅层输入字段定义：不接受 Python、Shell、URL 或表达式。私有流程的每一步必须调用已登记在 `allowed_tools` 中、并显式开放给用户流程的稳定只读 Tool。

## 1. 组件与职责

| 组件 | 职责 | 不负责 |
| --- | --- | --- |
| Planner / 路由策略 | 选择 `direct_llm`、RAG、脚本或受限 Agent 通道，编译静态 DAG | 绕过权限直接执行工具 |
| Tool Registry | 发布可直接执行的原子工具 Schema、领域、权限和结果契约 | 承载多步业务流程 |
| Workflow Skill Registry | 发布组合工具的流程、输入和 `allowed_tools`；开发者 Skill 公共、用户 Skill 私有 | 直接进入 Function Calling |
| 模型 | 根据用户目标、上下文和可用工具描述提出工具调用 | 自行扩大工具权限或伪造结果 |
| Skill Executor / Gateway | 校验、授权、执行、记录 span 和结果 | 将失败伪装成成功 |
| Result Verifier | 校验结果结构、来源和风险，必要时警告或升级 | 自动放宽安全限制 |

## 2. 工具注册契约

每个 Tool 至少声明：`name`、`description`、`domain`、`intent_tags`、`use_when`、`do_not_use_when`、`selection_examples`、JSON 参数 Schema、读/写权限、资源范围、超时、重试策略和结果契约。开发者 Workflow Skill 另声明 `allowed_tools` 与组合步骤，但不提供 Function Calling Schema；其 Python 文件仅能置于 `plugins/workflows/developer/`。用户 Workflow Skill 由 API 保存为数据库中的声明式步骤，默认绑定 `user_id`、仅所有者可见，不能上传 Python、Shell 或 URL；步骤只能引用显式标记 `user_workflow_allowed=true` 的稳定只读 Tool。模型不能通过 URL、Python 片段或自然语言临时创造新工具。

名称采用可读的 `动词_对象[_限定]` 形式，避免 `process_data`、`get_info` 一类泛名。描述采用三层契约：一句话能力、适用条件（含正例）、不适用条件（点名相邻 Skill）。例如 `web_search` 会明确排除 `read_document`、`query_knowledge` 和 `get_datetime` 的职责边界；因此“我今天的待办”不会因为含有“今天”而触发网页搜索。

## 3. 模型为何选择工具

模型按下面顺序收敛，不是按关键词强制调用：

1. 先判断已有对话、附件、知识库或模型自身生成是否已足够；足够则直接回答。
2. 仅在缺少某项外部能力时，先按 Tool 的 `do_not_use_when` 排除不相干或相邻能力，再按 `domain`、`intent_tags`、`use_when`、用户授权与运行时可用性筛选候选。
3. 优先选择范围最窄、结果最可验证的 Tool：精确算术优先 `calculator`（不得让模型口算）；系统时间优先 `get_datetime`；明确打开本机应用优先 `open_app`；多文档优先 `inspect_document_set/read_document`；天气/行情应优先未来接入的垂直供应商 Tool。
4. 语义相似度只能为候选排序，不能跨越权限、写开关、文档范围或审批边界。
5. 聊天通道只把当前请求相关的 Top-K（默认最多 5 个）候选 Schema 注入模型；没有支持证据时不暴露任何工具，直接回答。办公 ReAct **每轮**结合最新工具观察和失败工具刷新候选池；聊天保持单请求候选池。无唯一目标、参数不足或不确定时，澄清或说明边界；不通过“多调用一个工具”猜测。

### 3.1 候选池也是受治理的路由层

候选池只从 `ToolRegistry` 构建；`SkillRegistry` 中的组合流程由 Planner/Worker 选择后，通过 `workflow_runner` 逐步调用 Tool。

候选召回与模型选择是两个独立阶段，不能只根据最终是否调用工具判断正确性：

| 阶段 | 责任 | 指标 / 证据 |
| --- | --- | --- |
| Candidate recall | 从合法 Tool 池召回并注入 Top-K | `candidate_recall@K`、错误注入率、低置信告警 |
| Model selection | 模型在已注入候选中选择或不选择工具 | `selection_accuracy_given_candidates`、不必要调用率 |

候选 trace 还记录 `top_score`、`second_score`、`score_margin` 和 `ambiguous`。margin 使用过滤后的合法候选分数计算；即使只注入一个候选，也会从合法池保留第二名用于歧义评估。`SKILL_CANDIDATE_MARGIN_THRESHOLD` 以下视为近似并列：只读请求保留受限候选并记录告警，明确工具意图或涉及写入/外部系统时停止工具调用并要求澄清。

选择 trace 仅记录场景、轮次、`routing_mode`、候选 `name/version/score/bootstrap/availability_hint`、最终调用和未调用候选；不会记录用户原文、提示词、参数、思维链或工具正文。`routing_mode=semantic` 表示索引已就绪，`lexical_fallback` 表示索引未就绪或故障；后者必须由监控统计，不能静默混用。`availability_hint=circuit_breaker` 表示已授权外部 MCP 工具暂时不可用，但仍可被模型看见并得到受控错误，而不是被静默摘除。办公 DAG 将 trace 写入 `tool_metadata.selection_traces`，聊天写入监控事件。若请求有明确工具意图（如“联网并给网页来源”“从知识库查”“现在几点”）而候选为空或低于 `SKILL_CANDIDATE_LOW_CONFIDENCE_SCORE`，系统告警并禁止模型伪称已经核验；该告警不会强制调用工具。

新 Tool 可声明 `bootstrap_intents + bootstrap_until`：仅在有效日期内且命中限定意图时优先进入候选池，绝不全量注入；到期自动回归普通排序。到期前三天必须观察候选命中/选择率告警，而不是无条件续期。

### `web_search` 的特殊规则

`web_search` 仅用于公开网页事实、新闻、政策调研，且用户明确要求联网/来源或回答确需核实公开外部信息。它不用于用户的任务状态、私有数据、聊天历史、上传文件、知识库、总结改写、创作、计算；“今天/当前/实时/天气”单独出现也不构成调用理由。前端显式 `web_search=true` 只是把“用户偏好使用公开来源”提供给模型，仍不会绕过 ToolNode 直接预取；默认状态同样由模型在受控工具循环中决定。

## 4. 调用前的确定性门禁

模型提出调用后，执行器依次检查参数 Schema、Workflow Skill 的 Tool allowlist、用户/场景权限、资源声明和文档归属。写操作还要检查写开关、审批状态、上游结果哈希指纹与副作用 journal。任意门禁失败都会返回结构化错误，工具体不会执行。

## 5. 执行、结果回流与引用

工具返回统一的成功/失败状态、可展示内容、机器元数据、错误码和可选 citations。执行器会脱敏日志、记录参数哈希与 span；联网和检索来源转为 citation。成功结果作为受限 `tool` 消息回填模型上下文，供同一轮模型组织最终回答；DAG 模式下则按 `input_contract` 传给下游节点。

除原始数据外，回填还应携带 `decision_signals`：`result_count`、`confidence_hint`、`more_available`、`truncated` 和可选的 `refine_suggestion`。`confidence_hint` 必须是 `{level: low|medium|high, basis: [...]}`，且 `basis` 来自 citation 数、授权文档读取或供应商返回条数等可观察事实；没有可计算依据就不返回置信提示。这让模型知道“结果够不够、应继续读还是缩小查询”，而非根据 UUID、堆栈或大段原始文本猜测。参数或权限失败也会回填下一步建议，明确禁止通过替代写工具绕过审批。模型只能引用实际返回的资料，不能把空结果或失败当作证据。

### 5.1 统一输出流水线

Skill 的原始返回通过 `normalize_skill_result` 归一化为 `ToolOutput` 信封：`status(success/partial/failed/empty)`、`data`、`content_type` 和 `OutputMeta`。元数据保存总大小、产物引用、摘要、质量提示与引用，工具不再负责拼接面向模型的长文本。

执行器随后按工具声明的 `output_contract` 和运行时预算调用统一流水线：`text` 按预算截断；`structured` 优先保留交付字段并过滤路径、token、secret；`artifact` 只回填摘要和不透明引用；`empty`、`partial` 作为正常状态交付；citations 最多展示 10 条短摘录。原始网页/知识片段留在服务端结果存储，不进入模型上下文。

历史插件只填写 `SkillResult(output=...)` 时自动归一化；新插件应声明 `content_type`、预算和溢出策略。统一入口是 `app/services/tool_output_pipeline.py`，`tool_output_projection.py` 仅作为兼容调用，避免不同工具循环产生不同裁剪规则。

## 6. 失败、超时和审批

- 参数不完整、无权限、无匹配工具：澄清或返回明确错误码。
- 网络、额度、供应商故障：工具返回不可用；默认不以模型记忆伪造“实时”结论。
- 节点超时：运行器强制取消，进入 `interrupted` 或升级路径，lease 不会永久占用。
- 写工具：PostgreSQL effect journal 先写 `intent`，执行成功后写 `confirm`；journal 不可用即 fail-closed。有 intent 无 confirm 的恢复记录为 `EFFECT_UNCERTAIN`，禁止自动重试。
- 审批：外发、系统命令和写入在执行前暂停；审批哈希覆盖工具、参数和上游结果链，内容变化后旧审批失效。

## 7. 两个示例

### 多文档事实定位

`inspect_document_set → read_document → direct_llm`。候选唯一且高置信时读目标文件；候选接近则升级受限 React。所有 `doc_id` 均来自服务器注入的已授权范围，不能由模型扩权。

### 公开网页调研

用户说“请联网搜索本周发布的政策并给来源”。模型可提出 `web_search(query)`；Gateway 返回 URL、标题和摘录，随后模型依据这些 citation 作答。用户说“我今天的待办还有哪些”时不应联网，应只使用任务/对话上下文或澄清。

## 8. 新增 Skill 检查单

新增工具前必须完成：最小权限设计、Pydantic/JSON Schema、枚举化的有限参数、每个字段的正例说明、超时与限流、错误码、`decision_signals`/结果引用契约、日志与 span、读写及审批分类、单元测试和失败测试。涉及外部副作用时必须接入 effect journal；涉及用户文件时必须由服务端范围授权。原子 Tool 放入 `plugins/tools/`，组合流程放入 `plugins/workflows/developer/`，二者不得混放。

每新增或修改一个 Skill，都要补“选择评测”而不仅是执行测试：至少一条应调用正例，以及一条最易混淆的 `must_not_call` 反例。CI 以零执行 fixture 分别检查候选召回和给定候选后的选择约束；观察指标为候选召回率、选择准确率、应触发率和误触发率。Skill 的 `version` 会进入候选 trace 与评测基线，Schema/边界变更必须提升版本并重建基线。

注册时执行静态 lint：`handoff_to`、`conflicts_with`、`preferred_over` 的引用必须存在；bootstrap 必须同时给出限定意图和有效 ISO 到期日；两个 Skill 的 `intent_tags` 高度重叠时，至少一方必须声明结构化关系。模型看到的候选选择边界由这些注册字段编译生成，避免另一份手写提示词逐渐漂移。
