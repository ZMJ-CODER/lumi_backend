# 路由策略迁移

## 目标与边界

将稳定的“任务形态 -> 执行通道”判断从 `task_routing.py` 的条件分支逐步迁移为受限的
部署期 YAML 数据。核心仍负责：授权、场景白名单、风险确认、DAG 编译、资源锁和执行；
策略不能定义代码、导入模块、调用工具或放宽安全边界。

这不是热配置系统。v1 在进程启动时只加载一次策略文件；变更策略等同代码变更，必须经过
测试、评审和重新部署。

## 稳定契约

`RoutingFeatureSnapshot` 是路由策略的唯一输入，当前 schema 为 v1。规则语言、校验和匹配器位于
`packages/orchestration/src/lumi_orch/policy/`；可引用的业务特征在
`app/agents/orchestration/policy/features.py::ROUTING_FEATURES` 注册，并声明类型、计算 owner
和适用策略域。未经注册的特征、任意用户字段、前端展示字段和原始请求文本不能出现在 YAML。

当前可用操作符严格按特征类型限制：

| 特征类型 | 操作符 |
| --- | --- |
| `bool` | `eq` |
| `int` | `eq`、`gte`、`lte` |
| `str_set` | `contains` |

规则只支持 AND 条件，不支持 OR、嵌套表达式、正则、模板或 Python 表达式。复杂语言判断仍在
特征计算代码中完成，避免 YAML 变成不可测的编程语言。

```yaml
version: 1
rules:
  - id: explicit_single_file_conversion
    priority: 100
    when:
      - feature: has_explicit_file_operation
        op: eq
        value: true
    channel: deterministic_script
    reason_code: explicit_file_conversion
```

加载期会拒绝未知特征、错误类型/操作符、重复 ID/优先级、未知 hook，以及可能重叠但输出不同且
未声明 `overrides` 的规则。加载失败记录 `ROUTING_POLICY_LOAD_FAILED` 并继续使用 legacy 路由。

## 扩展点

`policy/hooks.py` 定义三个受控协议：

- `PreRouteHook` 只能增加要求、提高风险或要求澄清；
- `NodePolicyHook` 只能收窄既有节点参数和资源范围；
- `ResultVerifierHook` 只能给出 `pass / warning / escalation / reject` verdict。

YAML 仅能引用显式注册的 hook 名称，禁止 dotted path 或运行时发现。Hook 不能新增 Skill、
权限或写能力。

## 迁移与验收

| 阶段 | 范围 | 切换条件 |
| --- | --- | --- |
| 1，已完成 | 特征快照、schema、lint、影子模式和单文件转换 canary | 专用策略/规划器回归通过；`shadow` 不改变旧路由。 |
| 2，进行中 | 多文档事实定位 -> `agent + document_targeting` | 已接入影子计算；钩子只能声明 `document_discovery` 和 `scoped_document_read` 要求。至少 7 天影子数据后，所有预期差异人工归类，意外差异率不超过 0.5%，且无权限/副作用退化。 |
| 3，已完成 | 迁移 `task_routing.py` 的原子通道路由表 | 单文件转换、多文档定位、显式 RAG、已授权检索、通用 agent 协调及默认直答均已策略化；安全特征和执行授权仍在代码。 |
| 4，进行中 | 迁移动作词表与 TCA 阈值 | TCA v1 数值和固定动作/对象词典已进入 YAML；特征提取、否定识别和澄清边界仍保留代码与独立回归集。 |

`tca_rules.yaml` 只允许五项权重（总和必须为 1）和有限的 0-1 阈值；不支持
正则、表达式、文本提示词或路由动作。加载失败会记录 `TCA_POLICY_LOAD_FAILED` 并保持
内置基线，因此一份错误策略不会阻塞办公提交。

影子模式只计算 feature -> rule -> decision，不生成第二个计划、不请求 LLM、不调用工具、不执行
节点。命中带 hook 的规则时，审计还会记录受控 requirement 和固定 metadata；它们不能声明
新工具、新权限或写能力。日志只记录 rule ID、策略摘要、旧/新通道和安全特征，不记录用户原文。

部署变量：

```text
AGENT_ROUTING_POLICY_MODE=legacy|shadow|enforce
AGENT_ROUTING_POLICY_PATH=config/agent_policies/routing_rules.yaml
AGENT_TCA_POLICY_PATH=config/agent_policies/tca_rules.yaml
AGENT_ROUTING_LEXICON_PATH=config/agent_policies/routing_lexicon.yaml
AGENT_PLANNING_POLICY_PATH=config/agent_policies/planning_rules.yaml
```

默认 `shadow`。只有完成对应灰度验收后，才允许将某批规则切为 `enforce`；动态 DAG、审批和
写资源的安全验证仍在策略引擎之后执行。

## 动作词典

`config/agent_policies/routing_lexicon.yaml` 是确定性意图脚手架的版本化
短语数据。它只能为固定动作/对象枚举补充触发表达；内核 schema 会拒绝未知动作、
未知对象、空或重复 marker。词典不能定义通道、工具、权限、Python/import、正则或提示词。

词典在进程启动加载；错误会记录 `ROUTING_LEXICON_LOAD_FAILED`，随后退回内置兼容词典，
不会因为配置资产损坏而中断用户请求。新增表达应先在
`tests/test_planner_routing_intent.py` 添加口语化回归，再修改 YAML。

## 规划快捷路径词典

`config/agent_policies/planning_rules.yaml` 管理模板、半结构和脚本快捷路径的
固定 marker 集合。它只能引用受内核 schema 限制的既有模板名称，不能定义 DAG 节点、
依赖、执行器、工具或审批。`intent.py` 仍拥有模板优先级、附件存在性、外部操作拦截、
输出契约和最终模板构造；因此更换词典不会扩大任意能力。

该策略加载失败会记录 `PLANNING_POLICY_LOAD_FAILED` 并使用内置基线。新 marker 的
变更必须附带分类回归，尤其是“多主题不被单一模板劫持”和“无附件不进入文档模板”。
