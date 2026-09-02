# Temporal 清单迁移

> 当前状态：本地 Spike / 灰度基础已接入；默认运行时仍是 `legacy`。

## 目的

把显式办公任务清单的长时间执行从 API 进程移到 Temporal Worker，先验证滚动批次、Worker 重启恢复、History 控制和凭据隔离。TCA、四通道路由、清单授权与清洗、`EscalationSignal`、`LangGraphNodeRunner`、MCP Gateway 不在这一步重写。

## 当前边界

`AGENT_ORCHESTRATION=manifest_temporal` 时，只有满足下列条件的清单会进入 Temporal：

- 已通过现有显式清单授权和静态校验；
- 所有原子项都是 `direct_llm` 或 `rag`；
- 没有 `subtasks`、脚本、生成文件、客户端动作、审批或 Agent 通道。

其他任务继续进入现有动态 DAG。该限制是故意的：L2 审批和 L3 替代子图尚未迁入 Workflow，不能在试点阶段削弱写操作的幂等与人工确认语义。

## 静态 DAG 扩展

静态 Temporal 运行时现在使用独立的资格策略：普通静态 DAG 最多
`TEMPORAL_STATIC_MAX_NODES` 个节点（默认 12）。超过此窗口的完全物化纯读
DAG 可在 `TEMPORAL_STATIC_LONG_DAG_ENABLED=true` 时进入专用路径（默认关闭，
待服务级演练后显式开启），受
`TEMPORAL_STATIC_LONG_DAG_MAX_NODES`（默认 64）限制；允许已审核的纯读
`direct_llm`、`retrieval`、`document_targeting`、`web_research`、
`office_text`、`office_research`，以及 `office_doc` 的 `read/analyze/edit`
模式；另允许经过审查的 `office_todo` 和 `office_calendar`。带副作用的
节点只有在规划器显式设置审批、生成幂等键并通过 agent 白名单时才进入
Temporal。Workflow 通过 `approve_task` Signal 等待人工确认，Activity
继续复用 effect-journal 和资源锁。动态升级信号、ReAct 和未审批写操作仍由
Legacy 执行；逻辑计划另有下文限定的纯读试点。长 DAG 路径进一步拒绝全部审批和副作用节点，
不会把 effect-journal 恢复语义跨 Workflow Run 拆分。

入口会把 `eligible/code/detail` 写入 `job.routing.temporal_static_eligibility`，
便于观察哪些任务仍留在 Legacy 以及拒绝原因。静态候选不会进入逻辑计划
滚动物化；Temporal 不可用时，仅对尚未提交的任务回退 Legacy。

## 数据边界

`ManifestWorkflow` 的输入和 Continue-As-New 状态只包含 `job_id`、批次计数、超时和控制标志。完整 Job、清单、节点结果和进度仍持久化在 Redis；BYOK API key 只保存在短 TTL Redis bridge，绝不进入 Temporal history。

每批由 `run_manifest_batch_activity` 执行。Activity 内复用既有 `execute_dag`、资源锁、通道限流、副作用日志及 `LangGraphNodeRunner`。Activity 会 Heartbeat 并续租任务准入槽；Workflow 在指定批次数后 Continue-As-New，避免长清单的 history 无界增长。

若批次 Activity 的重试耗尽，Workflow 会调用轻量补偿 Activity，将 Redis 中仍在运行的 Job 收敛为 `failed` 并释放准入槽。这样 Workflow 层失败不会让前端永久卡在“执行中”；用户已取消或暂停的状态优先，不会被该补偿覆盖。

## 长静态 DAG：Child Workflow 与 Continue-As-New

50+ 节点试点只覆盖已冻结的纯读 `JobSpec`。父 `AgentDagWorkflow` 仍负责依赖判断、
并发窗口、暂停、取消和终态收敛；每个节点由 `NodeExecutionWorkflow` 子工作流调度一次
`execute_node_activity`，节点级 Activity 重试不会污染父工作流的调度状态。Child Workflow
不读取 Redis、数据库、时间或网络，所有外部 I/O 仍在 Activity 内执行。

父工作流在并发窗口收敛、且累计完成 `TEMPORAL_STATIC_CONTINUE_AS_NEW_AFTER_NODES`
（默认 20）个节点后 `Continue-As-New`。下一代输入仍携带原始带指纹 `execution_spec`，
但已完成节点只保留状态、错误审计字段和 `result_ref`（`id + sha256`），不保留正文、模型输出
或 API key。继续执行时，依赖节点和最终汇总 Activity 按用户域解析并校验该引用。若任何已完成
节点无法获得有效引用，则不切代，宁可保留当前 History，也不丢失依赖上下文。

这使已完成节点不会在新一代 Run 重放，暂停/取消仍由父工作流统一处理；Replan 计数和当前
冻结规格也随输入保留。静态重规划仍只允许纯读替代子图，并且在长 DAG 中同样受该限制。

相关开关：

```text
TEMPORAL_STATIC_LONG_DAG_ENABLED=false
TEMPORAL_STATIC_LONG_DAG_MAX_NODES=64
TEMPORAL_STATIC_CONTINUE_AS_NEW_AFTER_NODES=20
TEMPORAL_STATIC_CHILD_WORKFLOW_ENABLED=true
```

开启前至少完成：50+ 节点纯读 DAG 的端到端执行、节点运行中重启 Worker 后恢复、
Continue-As-New 后不重放已完成节点，以及 Workflow History 不含正文或凭据的检查。

## 本地启动

```powershell
docker compose -f docker-compose.yml -f docker-compose.temporal.yml --profile temporal up -d
```

Temporal UI：`http://localhost:8233`。本地 `.env` 需显式设置：

```text
AGENT_ORCHESTRATION=manifest_temporal
TEMPORAL_RUN_WORKER_INPROCESS=false
TEMPORAL_ADDRESS=temporal:7233
```

在宿主机直接启动 API/Worker 时使用 `TEMPORAL_ADDRESS=127.0.0.1:7233`；容器内必须使用 `temporal:7233`。

## Spike 验收

1. 用 50 项纯读清单执行，确认每批游标单调增加，已完成项不重放。
2. 在 Activity 运行时停止 `temporal-worker`，30 秒内恢复后确认同一幂等键未重复提交。
3. 使用超过 500 项的模拟清单，确认 Continue-As-New 后游标与状态保留正确。
4. 检查 Workflow history：不应出现文档正文、模型输出全文或 API key。
5. 验证多个 Worker 时通道上限和 Redis 资源锁仍保持有效。

静态 DAG 的人工审批节点已支持通过 Temporal `approve_task` Signal 恢复：审批前由
Workflow 生成工具名、规范化参数、上游结果哈希和确认指纹；审批后将确认上下文写入
Workflow 节点快照，Activity 继续执行统一的 `intent → confirm` effect-journal。
若 Temporal Worker 不可达，控制面报错且不会把已提交任务切回 Legacy。动态 ReAct、
L3 替代子图和未审批写操作仍保留在 Legacy；逻辑计划只迁移下文定义的纯读子集，
其余逻辑计划继续走 Legacy，待分别完成等价语义验收后再迁移。

## 运行时统一契约与灰度

提交阶段会将校验后的应用 `Job` 冻结为内核 `JobSpec`：每个 `NodeSpec` 固化 agent、参数、
依赖、资源声明、审批和幂等键，并对整个规格计算 SHA-256 指纹。`ExecutionBackend`、
`RuntimeGateway`、Legacy 和 Temporal 都以该规格为执行输入；`NodeResult`/`JobSnapshot`
作为后端无关的结果读取契约。Temporal 在 Workflow 开始前校验指纹，再从冻结的节点列表调度，
不会因 API/Redis 中的可变展示快照改变已提交操作。

灰度开关为 `TEMPORAL_STATIC_ALLOWLIST`、`TEMPORAL_STATIC_PERCENTAGE` 和
`TEMPORAL_STATIC_TASK_TYPES`。白名单优先；无白名单时按 `job_id` 稳定散列命中比例。建议先以
白名单和 5% 只读任务观察 Activity 重试、effect-journal 与 Workflow 查询一致性，再扩大比例。
正在执行的 Legacy 任务不迁移；已提交的 Temporal 任务也不回退到 Legacy。

## 静态纯读 Replan 试点

静态纯读 Temporal 任务已支持一次受限的失败重规划：当节点返回
`metadata.recovery.replan_required=true` 且当前并发窗口已经收敛，Workflow 调用
`replan_static_job_activity`。该 Activity 读取 Redis 短 TTL 的规划上下文、调用 Planner 并执行
原有编译校验，只返回新的带 SHA-256 指纹的 `JobSpec`；Workflow 不调用模型，只验证指纹、
替代未完成旧节点并挂载新节点。替代规格必须再次通过静态 Temporal 白名单，且不能包含审批、
资源写声明或幂等键。

默认 `TEMPORAL_STATIC_MAX_REPLANS=1`。Redis 上下文桥接不可用时，原静态任务仍可执行，
但失败后记录 `temporal_replan_blocked=context_unavailable`，不会把任务切回 Legacy。该试点不
覆盖 ReAct、外部 MCP 写操作或已审批副作用；这些路径仍由 Legacy 承担。

## 纯读动态 DAG（逻辑计划）试点

滚动逻辑计划把完整图保存在 Redis，`Job.nodes` 只保存当前可执行前沿。因此它
不能作为静态 `AgentDagWorkflow` 的输入。新增 `LogicalReadWorkflow` 只接收
`job_id`、Activity 心跳/超时和 Continue-As-New 前沿计数；完整计划、节点参数、
文档引用、节点结果和 BYOK key 都不进入 Workflow History。

每轮由 `run_logical_read_frontier_activity` 完成：加载 Job 与完整逻辑计划、校验
不可变执行指纹、执行当前前沿、把结果以 owner-scoped `result_ref` 提交到计划、
物化下一前沿并写回 Job。Workflow 只处理暂停、取消、Activity 重试和每
`TEMPORAL_LOGICAL_READ_CONTINUE_AFTER_FRONTIERS` 次前沿的 Continue-As-New。

### 骨架插槽与运行期补图

逻辑计划可以包含 Planner 显式声明的 `ExpansionSlot`。普通节点完成而插槽的上游依赖
已经满足时，Activity 将 Job 写为 `paused + scheduler_waiting_slots`，不会提前写为
`completed`。`plan_patch_available` Signal 只作为唤醒通知：补丁先通过调度层校验并写入
Redis，Workflow 收到 Signal 后再安排下一轮 Activity 读取持久化计划。因此 Signal 重复、
延迟或不携带正文不会改变图定义。

补丁必须携带当前 `base_revision` 和唯一 `patch_id`，只能扩展已就绪的插槽，并受插槽的
`max_nodes`、Worker 白名单和 `allow_effects` 限制。新增节点仍做完整 DAG/必需参数校验；
副作用节点还必须带审批、幂等键和资源声明。该机制已经有离线契约测试；Temporal 服务级
恢复、Signal 丢失和跨进程并发补图演练仍属于启用前验收项。

调度服务会对每个 `job_id` 同时获取本进程互斥锁和 Redis 短租约锁。前者避免同一 API
进程内重复处理，后者保证多个 API/Worker 进程不会同时基于同一计划版本附加补丁；已初始化
Redis 但租约不可用时请求会失败关闭，不能退化为可能覆盖共享计划的本地锁。

当上游结果决定下游数量时，LangGraph `Send` 可在 `prepare_plan_patch_candidate` 节点对每个
候选节点做独立规范化；所有结果必须先收敛，再由
`build_langgraph_plan_patch()` 生成**一份** `source=langgraph` 的补丁，并调用
`commit_langgraph_plan_patch()`。`Send` 分支不携带任务归属或执行权限，不能直接调用
Worker/Skill/MCP；计划版本、插槽、审批和 effect-journal 仍只由调度层和执行层控制。

准入策略检查完整计划，不能因为当前前沿是只读就掩盖后续节点：仅允许
`direct_llm`、`retrieval`、`document_targeting`、`web_research`、`office_text`、
`office_research`、以及 `office_doc(read/analyze)`；拒绝 ReAct、审批、资源写
声明、幂等键、`office_doc(edit)`、待办/日历写动作和未审核的逻辑重规划历史。

失败前沿先由可重试的前沿 Activity 持久化，再由 Workflow 以 **单次**
`replan_logical_read_activity` 调用 Planner。该 Activity 从 Redis 短 TTL 上下文读取
同一份授权文档范围，编译替代尾部，并再次按完整计划校验纯读策略；只有成功后才替换
未完成节点并重封执行指纹。LLM 重规划 Activity 不设置 Temporal 重试，避免 Activity
至少一次语义产生两个不同的替代尾部。默认最多
`TEMPORAL_LOGICAL_READ_MAX_REPLANS=1`；失败或策略拒绝时任务收敛为失败，不回退给 Legacy。

默认关闭，配置如下：

```text
TEMPORAL_LOGICAL_READ_ENABLED=false
TEMPORAL_LOGICAL_READ_ALLOWLIST=
TEMPORAL_LOGICAL_READ_PERCENTAGE=0
TEMPORAL_LOGICAL_READ_TASK_TYPES=
TEMPORAL_LOGICAL_READ_TASK_QUEUE=lumi-logical-read
TEMPORAL_LOGICAL_READ_CONTINUE_AFTER_FRONTIERS=20
TEMPORAL_LOGICAL_READ_MAX_REPLANS=1
```

## 已审批副作用动态 DAG（逻辑计划）试点

`LogicalEffectsWorkflow` 是与 `LogicalReadWorkflow` 分离的第二条逻辑计划运行时，
默认同样关闭。它只处理在规划阶段已经明确声明的写节点：节点必须同时具备经过审核的
agent、`approval=true`、`idempotency_key` 和写资源声明。纯读逻辑计划不会被这条路径
接管，仍按纯读开关进入 `temporal_logical_read` 或 Legacy。

Workflow 输入只包含 `job_id`、心跳/超时和 Continue-As-New 计数；完整计划、节点正文、
工具参数、结果和模型密钥继续保存在 Redis/结果仓。Workflow 只做前沿推进、暂停、取消、
审批 Signal、Activity 重试和 History 截断。LLM、Redis/PostgreSQL、文件、MCP、
Embedding/OCR 以及所有外部副作用均仅能在 Activity 中发生。

审批由控制面先调用 `ApprovalService.resolve()` 持久化决定，再发送
`approve_task(node_id, approved)` Signal 唤醒 Workflow。Activity 将该 Signal 仅作为
已持久化决定的确认回执：它不会自行批准节点，也不会为其他节点解释旧 Signal。批准后
仍以原确认指纹和原幂等键重新进入当前节点，执行层照常执行 PostgreSQL
`intent → confirm`、Redis 资源锁和 effect-journal 查询；Activity 至少一次重试时，
已存在 `confirm` 的效果直接复用，不重复写入。

等待审批时，Workflow 只保存确定性的等待时长并设置 Temporal Timer；Timer 到期后由
`expire_logical_effects_approval_activity` 重新读取 Redis Job，按持久化的
`approval_expires_at` 原子校验并写入 `APPROVAL_TIMEOUT`。因此 Workflow 不读取墙钟，
而迟到的 Signal 也不会覆盖已超时的终态。

该路径不支持 ReAct、自动 LLM 重规划、未审批写操作或无幂等键的副作用。失败会收敛为
可审计终态，不回退 Legacy；已经提交 Temporal 的任务在 Worker 不可达时，控制面明确报错，
不会由 API 进程重新执行。

```text
TEMPORAL_LOGICAL_EFFECTS_ENABLED=false
TEMPORAL_LOGICAL_EFFECTS_ALLOWLIST=
TEMPORAL_LOGICAL_EFFECTS_PERCENTAGE=0
TEMPORAL_LOGICAL_EFFECTS_TASK_TYPES=
TEMPORAL_LOGICAL_EFFECTS_TASK_QUEUE=lumi-logical-effects
TEMPORAL_LOGICAL_EFFECTS_CONTINUE_AFTER_FRONTIERS=20
```

无论使用独立 Worker 入口，还是设置 `TEMPORAL_RUN_WORKER_INPROCESS=true`
让 API 进程托管 Worker，都必须同时注册静态 DAG、清单、纯读逻辑计划和已审批副作用
逻辑计划四条队列。不能只注册原有静态/清单队列：否则逻辑计划 Workflow 会被成功创建，
却因没有对应 Activity Worker 而一直停留在 Temporal 队列中。

启用前需在隔离环境完成：批准、拒绝、重复 Signal、Activity 重试、Worker 重启、
intent-only 恢复、资源锁续租失败和取消中的端到端验证；目前仓库只完成离线契约回归，
不构成生产启用依据。

### 本机开发服务验收记录（2026-09-01）

在 Windows 本机 `temporal server start-dev`、本机 Redis/PostgreSQL 和独立
`app.agents.orchestration.temporal.worker` 下完成了以下不执行真实写工具的验收。所有
Job、逻辑计划、Redis 测试键和 PostgreSQL journal 测试行在每项结束后清理。

| 场景 | 结果 | 观察点 |
| --- | --- | --- |
| 纯读/副作用 Workflow 空任务引用 | 通过 | 两条专用队列均能由 Activity 收敛为可查询失败终态。 |
| 等待审批后拒绝 | 通过 | Workflow 与 Redis Job 均为 `failed`，节点为 `skipped`，未进入写工具。 |
| 审批超时 | 通过 | Workflow Timer 唤醒 Activity，节点持久化为 `APPROVAL_TIMEOUT`。 |
| 等待审批时暂停、恢复、再拒绝 | 通过 | 控制 Signal 不会丢失审批等待，恢复后终态正常收敛。 |
| 等待审批时取消 | 通过 | Workflow 与 Redis Job 均为 `cancelled`，未执行节点为 `JOB_CANCELLED`。 |
| Worker 重启后拒绝 | 通过 | Workflow 从 History 恢复，重新注册 Worker 后可消费 Signal 并收敛。 |
| stale intent 恢复 | 通过 | 独立 PostgreSQL `intent` 测试行超过 grace 后变为 `uncertain/recovery_orphaned_intent`。 |
| Redis 写资源锁 | 通过 | 同一写资源的第二持有者在首持有者释放前无法进入，释放后才获得锁。 |
| 审批后的真实本地待办写入 | 通过 | `todo_manager(add)` 经 `ApprovalService.resolve → approve_task Signal → Activity` 后只写入 1 条；节点为 `committed`，journal 为 `confirmed`。 |
| 已确认写 Activity 的重复投递 | 通过 | 使用相同 `idempotency_key` 再次调用 Activity 时直接复用 journal 结果，待办仍为 1 条。 |
| 纯读动态插槽补图 | 通过 | 首前沿完成后任务进入 `scheduler_waiting_slots`；调度层持久化 `PlanPatch`、发送 `plan_patch_available` Signal，新增 `follow-up` 前沿由 `LogicalReadWorkflow` 执行完成。测试 Workflow History 为 33 条、约 4.5 KB，未携带节点正文或凭据。 |

上述结果证明本地 `todo_manager` 这一受控写适配器的审批、`intent → confirm` 和已确认
Activity 重试幂等可以协作；它不等价于任意 MCP/办公写工具都已验收。外部写工具仍须分别
验证其业务效果、Activity 中断时的 `intent-only → uncertain` 收敛和补偿策略，再考虑按
白名单开启灰度。

纯读动态插槽场景使用本机 `qwen2.5vl:7b` 执行两次固定短文本生成，不访问文档、
不调用工具、不产生写操作。它只验证计划补图与 Workflow 唤醒的运行期行为，不代表
复杂 Agent、外部 MCP 或写操作已通过同等验收。

启用时先设置用户白名单和非零比例，并完成以下服务级检查：同一 `patch_id` 的
服务级重放不重复新增节点、Activity 运行中 Worker 重启、Continue-As-New 后已提交
节点不重复执行、取消/暂停期间不物化下一前沿，以及 Workflow History 不含节点正文
或凭据。
