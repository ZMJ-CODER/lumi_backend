# 任务理解、路由与能力预检（方案 4）落地说明

本文对应《任务理解、路由与能力预检方案（整合版）》，记录**后端已落地的实现入口**、
接入开关、契约字段与验收对照。方案 1 管内容、方案 2 管通道、方案 3 管状态，
本方案管"干之前先问三个问题"：

1. 这是什么任务（`TaskProfile`）？
2. 怎么干（`Router`）？
3. 干得了吗（`Capability Preflight`）？

**两条硬约束**（贯穿全部实现）：

* `task_assessor` 是**唯一**意图评估入口——任何模块不得再次解析用户原文判断复杂度/意图；
* 意图 ≠ 能力——画像通过后必须过预检，预检失败**直接返回结构化状态，不调用主模型**。

## 一句话链路（已落地）

```
User Query
  → task_assessor（唯一评估入口）
  → Unified TaskProfile（contracts 为权威词表，内核同名词表）
  → Router（硬约束：intents 非空禁直聊/单读；高风险动作强制审批）
  → Capability Preflight（环境/权限/注册/插件启用；失败不调模型）
  → Tool Window（画像驱动的最小工具集）
  → LLM / Planner → Tool Execution
```

## 契约（`lumi_contracts.routing`）

| 模块 | 内容 |
| --- | --- |
| `task_profile.py` | `TaskProfile`：`intent_type` / `action_intents` / `target_scope` / `target_clarity` / `has_dependency` / `has_runtime_decision` / `required_capabilities` / `risk_level` / `approval_required` / `confidence` / `confidence_source` / `decision_reason_code`，以及 `needs_workspace` 与 `fingerprint()`（影子比对） |
| `route_decision.py` | `RouteDecision` **schema v2**（`schema_version=2`、`route_mode` / `action_intents` / `target_clarity` / `approval_required` / `needs_clarification` / `capability_preflight`…），`RouteModeV2` 与 `EXECUTION_MODE_VALUES`（与内核逐值对齐） |
| `capability_preflight.py` | `PreflightState` 九态（含 `SECURITY_BLOCKED`）、`PREFLIGHT_ERROR_CODES`（全部取自 UnifiedError 码表）、`PREFLIGHT_NEXT_ACTIONS`（`safe_next_action`）、`PERMANENT_PREFLIGHT_STATES`（硬拒不给重试入口）、`CapabilityPreflight` |
| `shadow.py` | `ShadowRecord` / `DiscrepancyType` / `classify_discrepancy` / `build_shadow_report`（影子模式的结构化差异与统计口径） |

动作意图刻意拆成 8 个（不合并成笼统的 `WRITE`）：创建/修改/删除的风险、审批策略与工具
窗口完全不同——`CREATE → workspace_write`、`MODIFY → workspace_navigator + workspace_edit`、
`DELETE → workspace_navigator + workspace_delete`。

## 内核（`lumi_orch`）

| 模块 | 变更 |
| --- | --- |
| `task_assessment.py` | `TaskProfile` 增加方案 4 的语义字段（加法，全部有默认值）；`effective_action_intents`（显式意图优先、旧 `side_effects` 唯一映射兜底）、`requires_approval`、`HIGH_RISK_SIDE_EFFECTS = {DELETE, EXECUTE, PUBLISH}` |
| `execution_router.py` | 三条硬约束（见下）；`RouteDecision` 增加 `approval_required` / `needs_clarification` / `action_intents` 与稳定原因码 |
| `safety_policy.py` | 任务级预检按**动作意图**也拦高风险动作；沙箱执行是唯一例外（可回滚的执行方式，收敛为 `ALLOW_SANDBOX_ONLY`） |

### Router 三条硬约束（§2.2）

| 约束 | 落点 | 说明 |
| --- | --- | --- |
| `action_intents` 非空 → 禁止 `DIRECT_CHAT` / `M1_ATOMIC_READ` | `route()` 模式选择 + 兜底修正 | **只读意图（READ/SEARCH）仍是原子读的正例**；被拦的是"旧词表判只读、新画像判写入" |
| `DELETE` / `EXECUTE` / `PUBLISH` → 必须审批或阻断 | `requires_approval` + `task_level_action` | 与置信度无关；`approval_required=True` 是挂起（走审批卡），不是失败 |
| 目标未知且有动作意图 → 澄清 | `target_clarity == UNKNOWN` | 澄清优先于模式选择：不猜路径、不进编排 |

> **最重要的回归场景**："旧词表判只读 + 新画像判 `CREATE`"必须进入
> `M1_ATOMIC_ACTION` 并注入 `workspace_write`——原因码 `ACTION_INTENTS_REQUIRE_ORCHESTRATION`
> 专门用于把它与旧路径区分开（见 `tests/orchestration/test_task_understanding_preflight.py`）。

## 路由快照（`route_snapshot.py`，schema **v2**）

* `route_decision.schema_version = 2`，新增 `intent_type` / `action_intents` / `target_scope` /
  `target_clarity` / `required_capabilities` / `approval_required` / `needs_clarification` /
  `confidence_source` / `decision_reason_code`；
* `capability_preflight` 位随快照落盘；**未预检时不写空对象**（`{}` 会被读成"预检过、结论为空"）；
* 旧画像继续只进 `compat.legacy_task_profile`，不占顶层名字；旧字段镜像保持不变；
* `STRICT_PROFILE_FIELDS` 同步扩展，SSE 的 `task_profile` 镜像自动带上新字段（**不改协议**）；
* Job 提交只读这份快照；任务全生命周期（含恢复）使用同一份。

## 能力预检（`app/agents/orchestration/capability_preflight*.py`）

检查顺序（短路，先便宜的、先硬的）：

```
0. 安全策略（SECURITY_BLOCKED，硬拒）
1. 目标澄清（NEEDS_CLARIFICATION）
2. 工作区绑定（DEPENDENCY_MISSING → DEPENDENCY_MISSING_WORKSPACE）
3. Provider / 能力提供方（CAPABILITY_UNAVAILABLE / PROVIDER_UNHEALTHY，含插件被禁用）
4. 用户权限（PERMISSION_DENIED，硬拒）
5. 工具注册 + 注入窗口非空（TOOL_NOT_REGISTERED / CAPABILITY_UNAVAILABLE）
6. 审批（APPROVAL_REQUIRED）
```

* **失败一律不调模型、不给空工具集**：`must_call_model=False` 是给调用方的唯一判据；
* 失败统一出口是 `preflight_snapshot()`：`status` / `error_code` / `safe_message` /
  **`safe_next_action`** / `retryable` / `needs_human` / `permanent` / `question` /
  `tool_window` / `required_capabilities` / `checks`；
* 语义区分铁律：`NEEDS_CLARIFICATION`（用户没说清 → 给选项）≠ `CAPABILITY_UNAVAILABLE`
  （环境缺 → 提示修环境）≠ `APPROVAL_REQUIRED`（可用但要确认 → 审批卡）≠
  `PERMISSION_DENIED` / `SECURITY_BLOCKED`（硬拒 → 不给重试入口）；
* **插件生命周期联动**【吸收 #1】：没有注入 Broker probe 时用注册表兜底
  （`plugin_capability_availability`）——插件被禁用/卸载 → 其能力直接
  `CAPABILITY_UNAVAILABLE`，在模型调用之前阻断；
* **只阻断不降级**【吸收 #5】：环境/权限/注册/插件层面的缺失换降级策略解决不了；
  模型能力失配仍由方案 1 的 Model Capability Router 处理（两层互不越界）。

## Tool Window（画像驱动的最小工具集）

`ACTION_TOOL_WINDOW` 是唯一映射表，工具名与 `app/agents/capabilities/catalog/legacy.py` 的注册名
**逐字一致**（测试断言，否则预检"通过"却注入不存在的工具）：

| 意图 | 工具窗口 |
| --- | --- |
| `READ` / `SEARCH` | `workspace_navigator` |
| `CREATE` | `workspace_write` |
| `MODIFY` | `workspace_navigator` + `workspace_edit` |
| `DELETE` | `workspace_navigator` + `workspace_delete` |
| `MOVE` | `workspace_navigator` + `workspace_move` |
| `EXECUTE` | `run_in_sandbox` + `python_exec` |
| `SEND` | `send_email` |

`build_tool_window()` / `tool_window_for_actions()` 支持只暴露**已注册**工具；预检失败时
工具窗口恒为空（由 `must_call_model=False` 阻断流程）。

## 影子模式（`app/services/task_shadow.py`，`INTEGRATION_SHADOW_MODE`）

* 新旧判定**同时运行**：记录 `legacy_requires_orchestration` /
  `profile_requires_orchestration` / `discrepancy_type` / `selected_route_source`（影子期
  固定 `legacy`）/ 两侧模式与原因码 / `confidence_source` / `assessor_ms` / 超时事实；
* 差异分类（`DiscrepancyType`）：`legacy_read_profile_write`（**最重要回归场景**）、
  `legacy_write_profile_read`、`legacy_simple_profile_complex`、
  `legacy_complex_profile_simple`、`assessor_fallback`（超时/非法 JSON → heuristic）；
* **任何非 `none` 的差异都以新画像为准**（`discrepancy_should_use_profile`）；影子期只记录
  不改行为，切换后旧词表只留诊断兜底；
* 记录只含**枚举与原因码**（不含用户原文/推理），落 Redis（有界 200 条/任务 + TTL），
  失败只记日志、**绝不影响任务执行**；
* 聚合报告 `build_shadow_report`：`diff_rate` / `timeout_rate` / `by_type` /
  平均分类耗时，用于判断"达标才切换"。

## 接入开关（默认全关，关掉即回到旧路径）

| 开关 | 作用 | 关闭时 |
| --- | --- | --- |
| `TASK_PROFILE_CANONICAL` | 用 canonical 画像的动作意图/目标范围/清晰度驱动路由 | 内核画像按旧 `side_effects` 推导（行为不变） |
| `CAPABILITY_PREFLIGHT_V2` | 统一预检服务接管（失败不调模型） | 历史 `task_preflight` 澄清门 |
| `INTEGRATION_SHADOW_MODE` | 新旧并行记录差异（不切换） | 不记录 |

路由硬约束与预检的**枚举/判定**本身与开关无关：开关只决定"由哪条路径产生画像/判定"。

## 验收对照（§8.2 / §8.3 / §10）

| 验收项 | 落点 |
| --- | --- |
| 只有一处解析用户原文做意图判断 | `task_assessor` 是唯一生产者；`task_shape` 的正则只在兼容适配器里（标记为 Phase 6 清理项） |
| "创建文件"任务注入 `workspace_write` 且经 Broker | `ACTION_TOOL_WINDOW[CREATE]` + `test_action_intents_block_direct_chat_and_atomic_read` |
| 纯生成不注入写工具、不触发审批 | `test_action_intents_block_direct_chat_and_atomic_read`（无意图 → `DIRECT_CHAT`）+ `requires_approval` 默认 False |
| 目标不明确返回结构化澄清状态与选项 | `test_unknown_target_asks_for_clarification_instead_of_guessing`、`PREFLIGHT_NEXT_ACTIONS[PROVIDE_TARGET]` |
| 环境缺失时不调模型、无空工具集、有 `safe_next_action` | `test_preflight_snapshot_exposes_actionable_next_step`、`test_plugin_disabled_capability_fails_preflight_before_the_model` |
| 旧词表与新画像冲突以新画像为准 | `test_legacy_read_new_profile_write_uses_the_profile` + 影子分类 `legacy_read_profile_write` |
| 高风险删除即使高置信度也进审批 | `test_high_risk_actions_require_approval_even_at_high_confidence` |
| 插件禁用后相关 capability 预检直接失败 | `test_plugin_disabled_capability_fails_preflight_before_the_model` |
| 恢复使用快照中的 TaskProfile | 方案 3 的 Job 快照承载 `route_decision.task_profile`（`schema_version=2`），恢复不重评 |
| 前后端只用稳定枚举 | `PreflightState` / `ActionIntent` / `RouteModeV2` 三张词表 + TS 导出同步 |

## 单一事实源的接入现状（Phase 3/4 已完成部分）

| 模块 | 现状 |
| --- | --- |
| `task_assessor` | **唯一**评估入口；`canonical_profile()` 是唯一画像生产者 |
| `task_router_adapter.plan_and_route` | 评估一次 → 合并 canonical §1.1 字段 → 路由 → 影子记录；`meta()` 带预检位 |
| `react_runner` 工具窗口 | **画像优先**：`action_intents` 直接决定注入哪些工作区能力（读/写/沙箱），关键词只在没有画像时兜底；`WorkerContext.task_profile` 由编排器注入（只读事实） |
| `execution_policy` | `TaskEntrySignals.action_intents` / `complexity_hint` / `needs_runtime_decision` 由画像注入；有画像时不再用 `_GOAL_WORDS` / `_HIGH_RISK` 解析用户原文 |
| `job_submission_service` | 先出 Router v2 决策，再让策略层**消费同一份画像**；没有画像时才退回 `task_shape` |
| `task_shape` | 降级为**纯适配器**：`shape_from_profile()` 投影画像，`assess_task_shape()` 标注 `source="legacy_regex"`（Phase 6 删除项） |
| `office_plan_selection_service` | 预检输入用 canonical 画像的真实 `action_intents` / `required_capabilities` |

## 预检状态链（前后端一体）

`preflight_control_frame()` 把预检结论翻成**既有** `control` 事件（不新增事件类型）：

| 预检状态 | `control.state` | 前端行为 |
| --- | --- | --- |
| `NEEDS_CLARIFICATION` | `waiting_clarification` | 展示问题 + **后端给的选项**（仅生成/创建/编辑/取消）；不是终态 |
| `APPROVAL_REQUIRED` | `waiting_approval` | 复用既有审批卡（与 `approval_required` 事件并存） |
| `DEPENDENCY_MISSING_WORKSPACE` / `CAPABILITY_UNAVAILABLE` / `PROVIDER_UNHEALTHY` / `TOOL_NOT_REGISTERED` | `blocked` | 按 `error_code` 给"可执行下一步" |
| `PERMISSION_DENIED` / `SECURITY_BLOCKED` | `blocked` | **硬拒**：只说明原因，不给重试入口 |

`control` 载荷随路由快照落盘（`routing.preflight_control`），实时帧与刷新恢复读同一份。
载荷字段（`phase` / `question` / `options` / `required_capabilities` / `tool_window` /
`must_call_model`）已在 `ControlPayload` 与事件白名单里登记——否则规范化路径会把它们裁掉。

## 前端对齐（`E:\javaidea\lumi`）

| 前端位置 | 对齐内容 |
| --- | --- |
| `src/services/taskStatus.js` | `PREFLIGHT_STATUS_LABELS` 补 `SECURITY_BLOCKED`；新增 `PREFLIGHT_ACTION_TOKENS` + `nextActionText()`（后端 `safe_next_action` 是 `BIND_WORKSPACE` 这类稳定 token，直接显示会露 token）；`describePreflight` 同时读 `next_action` / `safe_next_action`；`describeControl` 输出 `errorCode` / `hardDeny` / `question` / `options`，硬拒不显示下一步；同时认驼峰与蛇形字段 |
| `src/services/streamConsumer.js` | control 帧保留 `phase` / `error_code` / `safe_next_action` / `question` / `options`；`waiting_clarification` 落澄清态时**使用后端给的选项**（不再留空数组） |
| `src/services/routeDecision.js` | 预检通过时把 **tool window** 拼进状态链 detail（"可用工具：workspace_write"），满足验收"创建文件任务里必须出现写工具" |
| `packages/contracts/ts/lumi-contracts.d.ts` | 新增 `PreflightState` 九态、`CapabilityPreflight`、`ControlPayload`（含预检字段）、`RouteDecision` v2 字段、`TaskProfile` §1.1 字段 |
| `electron/task-status.smoke.cjs` / `stream-consumer.smoke.cjs` | 新增：token 翻译、硬拒不显示下一步、澄清选项、blocked/waiting_clarification 的终态与暂停语义 |

前端**不**自己判断复杂度/是否写入/该用哪个工具/是否允许执行——这些全部由后端返回；
`route_mode`（后端决策）与 `execution_mode`（用户交互概念）继续分开，不得混用。

## 能力名归一化与"没有可用 Provider"的精确归因（修复记录）

**缺陷**：目录 / 注册表 / 插件清单里存的是**无版本基名**（`workspace.read`），而画像
解析出的具体能力名带契约版本（`workspace.read@1`）。按字符串直接比对会把同一个能力
判成两个结果——曾出现"`workspace.read@1` 被判成缺少必需能力 / 没有可用的 Provider"，
把只读任务误阻断，看起来像"模型识别不到读工具"。

**修复**（`app/agents/capabilities/registry/resolver.py`）：

* `normalize_capability_name()`：唯一归一化入口（剥 `?` 可选前缀与 `@数字` 版本；
  `@beta` 这类非数字后缀不误伤）；
* `split_capability_version()`：解析 `@N` 契约版本。此前 resolver 用的是插件依赖的
  `parse_requirement`（只认 `>=`），`@1` 被静默忽略 → 现在两种写法都认；
* 静态可用性判定（`plugin_capability_availability`）与 Broker 的租约判定统一走归一化。

**归因**：Broker 现在区分"没有可用 Provider"的**真实原因**，而不是一律一句
"没有可用的 Provider"：

| 底层事实 | 含义 | `safe_next_action` |
| --- | --- | --- |
| `provider_not_connected` | 该能力从来没有设备注册过 | `CONNECT_PROVIDER` |
| `provider_binding_mismatch` | Provider 在线，但不属于当前工作区/会话 | `BIND_WORKSPACE` |
| `provider_unroutable` | 当前部署位置不允许该 Provider 执行 | `CHANGE_EXECUTION_PLACEMENT` |
| `provider_unhealthy` | 已有租约但过期 / 心跳中断 | `RETRY_PROVIDER` |

对外状态与错误码**契约不变**（仍归 `PROVIDER_UNHEALTHY`，可重试），只有下一步文案与
前端入口按原因细化（`PREFLIGHT_LOCAL_SURFACES`）。**没有可用 Provider 时预检仍然阻断**，
不调用模型、不给空工具集——`CAPABILITY_*` / `PROVIDER_*` 属环境缺失，换降级策略解决不了。

## 租约可见性与"缺能力"快照的时效性（修复记录 · 二）

上一条修好了**名字归一化**，但线上仍有"界面报缺少必需能力 / 没有可用的 Provider，
而同一次任务的工作区读取其实成功"。真因是另外两条，与名字无关：

**1）单例撕裂：Broker 看不到任何客户端注册（`app/agents/capabilities/broker/broker.py`）**

* 注册端点（`/capabilities/register`、`/heartbeat`、`/unregister`）注入的是
  `app.services.capability_lease.capability_lease_service`；
* 而 `capability_broker = CapabilityBroker()` 默认走
  `leases or CapabilityLeaseService(registry=...)`，**自建了另一个实例**；
* `select()` 只读该实例的进程内私有字典 → 提交期能力解析恒判
  `provider_not_connected`（"该能力尚无设备注册"），即使注册表里已经有 Provider。
  唯一的补漏点在 `dispatch.py` 的 `await refresh_from_redis()`，即只在**派发路径**生效。

修复：`capability_broker` 复用共享 `capability_lease_service`；Broker 增加唯一读取入口
`_visible_leases()`（= `CapabilityLeaseService.snapshot()`），`CapabilityLeaseService.snapshot()`
改为**始终并入进程内副本**（此前 Redis 缓存非空就整体丢弃本进程刚注册的租约），
`capability_snapshot()` 同样走该入口。

**2）门禁静默失效（`app/agents/skills/capability_route.py`）**

`execute_tool_call(capability_lease_service=None)` 把 `None` 一路传到
`CapabilityDispatchAdapter.dispatch` 的 `self._leases.snapshot()` → `AttributeError` →
被 `try_capability_route` 吞掉 → **每次工具调用**都打
`[capability] 路由门禁异常（落回旧路径）` 并绕过能力路由（`AGENT_CAPABILITY_ROUTING_MODE`
线上默认 `active`）。修复：缺省注入在 `try_capability_route` 内回落到共享租约服务，
只保留一处判定。

**3）提交期快照被当成实时结论（后端 + 前端）**

* 后端：`capability_resolution` 只在提交时写一次。现在同时落
  `capability_resolution_query`（重算输入）与 `capability_resolution_at`（时刻戳），
  并提供 `refresh_capability_resolution()`；`GET /jobs/{id}` 返回前按当前租约重算
  （失败保留旧快照并写 `capability_resolution_error`，绝不阻断详情）。
* 前端：**阻断权归预检**。能力快照缺失只作提示（`tone: warn`，不再是 `bad`），
  文案标注"提交时快照，尚未刷新；实际可用性以执行结果为准"；
  `describeRouteProgress` 在无预检结论时该步骤标 `current` 而不是 `blocked`；
  一旦 Job 已有**成功的读取结果**（`read_evidence` / `unified_read` / `read_count>0`），
  直接把该能力视为已证可用（`capability.proven`），不再提示缺能力并改显"实际可用"。

**4）读取链路故障被说成"没有资料"（`app/agents/roles/knowledge/workspace_coverage.py`）**

候选为空时原先一律返回 `WORKSPACE_NO_CANDIDATE` + "工作区里没有找到与目标相关的可读文件"，
而同文件把 `status=empty` 当成功、`notes`（含 `SEARCH_FAILED`/`LIST_FAILED`）在出口分支被丢弃。
现在：`discovery` 记录 search/list 的**真实结局**（成功/空/失败码）并随出口带出；
链路失败时错误码映射为既有恢复分类认识的 `MCP_UNAVAILABLE` / `CLIENT_OFFLINE`
（`WORKSPACE_NOT_BOUND` / `WORKSPACE_NOT_REGISTERED` / `WORKSPACE_DEVICE_OFFLINE`），
`retryable=true`，文案明确写"工作区读取链路不可用：…（原始码）"；
只有**确实没有候选**时才用原来的如实文案。

## 未在本轮改动范围内的部分（方案 Phase 6）

* **删除业务路由正则**：`task_shape.py` 与 `task_preflight.py` 的 `_EFFECT_RE` / `_TARGET_RE`
  仍是兼容兜底路径（`TASK_PROFILE_CANONICAL` 关闭时生效）。方案要求"全面切换后删除"，
  属于 Phase 6，需与影子模式达标报告一起执行；安全层危险命令/越权规则保留。
  `task_shape.TaskShape.source` 已能区分 `profile` / `legacy_regex`，便于按来源统计残余流量。
* **澄清交互的回填链路（§6.2）**：`control(state=waiting_clarification)` 已带问题与选项，
  `PROVIDE_TARGET` 已给出下一步；"用户补充目标 → 校验相对路径 → 重走预检 → 恢复任务"
  仍由既有的 `clarification_answer` 与恢复接口承担。
* **前端展示顺序（§7.3）**：`describeRouteProgress` 已按"路由 → 工作区 → 能力 → 审批 → 结果"
  组装，工具窗口已进 detail；气泡的视觉顺序调整属于渲染层。
