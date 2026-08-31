# 工具调用与路由成本评测

评测脚本位于 `scripts/evaluate_tool_routing.py`，标注集位于
`tests/fixtures/tool_routing_eval.jsonl`。它只测试**第一轮工具选择**：模型返回
Function Calling 后立即停止，绝不执行工具。因此文件写入、客户端桌面操作、邮件、
公网搜索和知识库读写都不会发生。

为后续多轮回放保留了 `EvaluationTool` 测试替身：它从生产 `ToolCapability` 原样
复制工具名、描述和参数 JSON Schema，但 `execute()` 永远只返回固定
`[evaluation] ... synthetic result`，不会分发到生产 Skill。评测脚本目前只进行首轮
选择；模型作出调用决定后，它只会执行一次相同名称的替身工具并在输出 JSON 的
`stub_execution` 字段记录结果，不会进入第二轮 LLM 或分发生产工具。这样可以先量化
“该不该调用 / 调哪个 / 参数是否对”，同时验证替身边界可用。

## 标注集规则

每行是一条 JSONL 记录：

```json
{
  "id": "calc-01",
  "scene": "chat",
  "query": "请精确计算 12873*47-912",
  "expected_tool": "calculator",
  "expected_params": {"expression": "12873*47-912"}
}
```

不应调用工具的反例使用：

```json
{
  "id": "negative-01",
  "scene": "chat",
  "query": "解释什么是机器学习",
  "expected_tool": null,
  "expected_params": {},
  "must_not_call": true
}
```

参数按 JSON AST 结构严格对比：对象键顺序无关，但字段名、数组顺序和值都必须一致。
不要用语义相似度掩盖参数错误。只有供应商会注入无害默认参数时，单条样本可显式
设置 `allow_extra_params: true`。

当前首版为 60 条：40 条正例（5 个聊天入口工具各 8 条）和 20 条反例。它是可运行
基线，不代表已覆盖全部低频办公或开发工具；扩大范围时，每个新增工具至少补 3 至 5
条正例和相邻工具的混淆例。

## 运行

先做离线 fixture 与报告链路验证（不调用模型，不能作为模型能力指标）：

```powershell
.venv\Scripts\python.exe scripts\evaluate_tool_routing.py --mode both
```

再用项目已配置的 LLM 进行真实选择评测：

```powershell
.venv\Scripts\python.exe scripts\evaluate_tool_routing.py --mode both --live --repeat 1
```

如果要按厂商实际价格折算成本，复制 `tests/fixtures/llm_pricing.example.json` 到
本地安全位置，填写当前模型的输入/输出每百万 token 单价，再传入：

```powershell
.venv\Scripts\python.exe scripts\evaluate_tool_routing.py --mode both --live `
  --pricing C:\safe\llm_pricing.json
```

结果写入被 Git 忽略的 `artifacts/tool-routing-eval/`：

- `baseline.json`：完整合法聊天工具集注入的对照组。
- `routed.json`：仅注入现有候选路由器选出工具的实验组。
- `comparison.json`：token 与金额降幅。
- `report.md`：可读摘要。

`baseline` 和 `routed` 都只比较同一个首轮选择任务，避免把候选召回、工具执行错误、
多轮 Agent 规划混成一个数字。

## 指标解释

- `candidate_recall_at_k`：正例中，正确工具进入实验组候选池的比例。它衡量候选路由。
- `selection_accuracy_given_candidates`：正确工具已进入候选池时，模型仍选对的比例。
- `tool_selection_accuracy`：全量样本工具是否选对；反例的“正确”是没有工具调用。
- `parameter_accuracy_given_correct_tool`：模型选对工具后，参数 JSON 是否严格匹配。
- `fully_correct_rate`：工具与参数均正确的比例。
- `false_call_rate`：仅在 `must_not_call` 反例中计算的误调用比例。

出现模型调用、网络或配置错误时，记录会保留在 JSON 中，但不会被并入模型选择、参数或
误调用分母；报告的 `valid / cases` 和 `errors` 必须先为全量成功，才可引用该轮数字。

直播模型返回的 `usage_metadata` 优先计入 token；供应商没有返回 usage 时，记录会标明
`estimated`，表示使用项目现有的字符估算兜底。价格按输入/输出分开计算：

```text
cost = prompt_tokens / 1_000_000 * input_price
     + completion_tokens / 1_000_000 * output_price
```

## 四级路由的端到端成本口径

本脚本的成本 A/B 是“全工具注入 vs 候选工具注入”，能量化当前工具治理的 token 影响。
它**不是**“所有任务强制 Agent vs 四级路由”的端到端成本数字：后者需要一组可安全执行的
任务回放，以及在每个路径上汇总 `LLMUsage` 的 `chat`、`plan`、`tool_decision`、`skill`
等分类。

做该实验时，应对同一任务集分别运行：

1. A 组：强制 `agent` 路径；
2. B 组：执行当前 `direct_llm` / `deterministic_script` / `rag` / `agent` 四级路由；
3. 为两组使用独立 eval user / run id，按 run id 汇总 prompt、completion、调用数；
4. 使用同一价格表换算，报告 `1 - cost_B / cost_A`，并保留每条任务路由轨迹。

在没有只读回放桩之前，切勿用带发送、写文件、修改外部系统的生产任务做强制 Agent
对照实验。

## 路由策略与扩展方式

候选召回与模型选择是分离的两级策略：

1. **候选召回（确定性、低成本）**：先按场景、角色、写操作开关和运行时可用性过滤，
   再利用每个 `Skill` 的 `domain`、`intent_tags`、`use_when`、冲突和替代关系进行
   词法/可选语义排序；仅保留有正向证据的 Top-K 工具。
2. **工具选择（LLM）**：只将候选池及其参数 Schema 注入模型，由模型选择一个工具并
   生成参数。`selection_accuracy_given_candidates` 专门衡量这一层。

路由器不维护“请求关键词 → 工具名”的硬编码表。新插件应优先在自己的 `Skill` 类声明
上述元数据；现有未迁移插件才由 `_OFFICE_REACT_ROUTING_METADATA` 兼容目录补齐。新增或
下线工具只会改变能力目录，选择器无需修改。每次补充意图短语时，应同时添加正例和相邻
反例，防止以扩大候选池为代价掩盖误调用。

新增聊天工具的最小接入项是：在插件类内填写 `domain`、3 至 8 个有区分度的
`intent_tags`、`use_when`、`do_not_use_when` 和 1 至 2 条 `selection_examples`，并在
`tests/fixtures/tool_routing_eval.jsonl` 添加至少 3 至 5 条正例及相邻能力的混淆/反例。
这属于插件的能力契约，而不是修改候选选择器；只有新增了全新的领域、授权规则或冲突关系
表达不了的选择规则时，才需要演进通用路由策略。

## 2026-08-29 真实模型评测结果（DeepSeek）

环境：`deepseek-chat`；60 条标注集（40 条正例、20 条不应调用工具反例）；每组重复 3 次，
共 180 次模型调用。所有测试均为首轮选择评测，**没有执行生产工具**，`valid_decision_count`
均为 180、`error_rate` 均为 0。

| 指标 | Baseline：全工具注入 | Routed：候选工具注入 | 变化 |
| --- | ---: | ---: | ---: |
| 候选召回率 | 100% | 95%（114/120） | -5pp |
| 候选池内选择准确率 | 100% | 100% | 持平 |
| 工具选择准确率 | 100% | 96.67% | -3.33pp |
| 参数准确率（选对工具后，AST 严格匹配） | 53.33% | 54.39% | +1.06pp |
| 完全正确率（工具和参数均正确） | 68.89% | 67.78% | -1.11pp |
| 误调用率（60 条反例） | 0% | 0% | 持平 |
| Prompt Token | 312,078 | 92,868 | **-70.24%** |
| Completion Token | 18,292 | 13,663 | **-25.31%** |
| 总 Token | 330,370 | 106,531 | **-67.75%** |

本次 Routed 的 4 个漏召回（3 次重复后的 2 个固定用例）为：`calc-02` 的“帮我算”及
`datetime-02` 的“几号”。这是候选召回元数据缺口，不是模型选择错误：正确工具进入候选池
时模型选择准确率为 100%。已在对应工具的 `intent_tags` 中补充多字意图短语 `帮我算` 与
`几号`，并添加回归测试；不添加泛化的单字 `今天`，以避免将“我今天的待办”等普通问题
误召回到时间工具。

### 优化后复测（2026-08-29，DeepSeek）

修改插件元数据后，Routed 模式复跑 3 次：180/180 有效、错误率 0。候选召回率和总体工具
选择准确率均恢复为 **100%**，60 条反例的误调用率保持 **0%**。本轮总 Token 为 117,102，
相对同批 Baseline 的 330,370 Token 降低 **64.55%**；Prompt Token 降低 **67.15%**。
完成率为 69.44%，参数严格准确率为 54.17%。后两项与改造前基本持平，说明本次改动只修复
候选召回，没有掩盖或恶化参数生成问题。

当前最主要的独立优化项是参数生成：两组的严格参数准确率都约 54%。这不是候选路由造成的，
应从工具 Schema、参数示例及允许的默认值策略入手，单独建参数错误分类后再优化。

部署后运行顺序：

```powershell
# 1. 先确认评测替身和报告链路；不触发模型或生产工具。
.venv\Scripts\python.exe scripts\evaluate_tool_routing.py --mode both

# 2. 再通过已部署、可连通的 LLM 配置测三轮；仍只运行 EvaluationTool 替身。
.venv\Scripts\python.exe scripts\evaluate_tool_routing.py --mode both --live --repeat 3
```

第 2 步如果 `valid / cases` 不是全量，先收集 `records[*].error` 解决模型连通性或
配置问题，再重跑；不要使用那一轮的准确率、误调用率或成本数字。

参数严格准确率的后续优化与四级路由/SciFact 的独立评测口径见
[`ROUTING_AND_RETRIEVAL_EVALUATION.md`](ROUTING_AND_RETRIEVAL_EVALUATION.md)。特别注意：
参数契约优化后的真实 DeepSeek 复测为 71.43% 严格参数准确率（相对优化前 Routed 的
54.17% 为 +17.26pp）；四级离线回归集的均衡比例也不能代替生产真实路由占比。
