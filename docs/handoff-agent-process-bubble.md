# 交接：Agent 任务 AI 回复气泡（后端剩余项）

> 给下一个会话/agent 的自包含交接。读完这一份即可继续，不需要上一轮对话上下文。
> 仓库：`E:\pythonpycharm\lumi_backend`（后端）。前端在另一个仓库：`E:\javaidea\lumi`（**不要在这里改**）。

## 1. 目标与边界

实现「Agent 任务 AI 回复气泡」：Agent/步骤协议任务用**执行过程气泡**（过程日志 + 最终结果两区域），
普通聊天继续用现有气泡。硬约束（用户原话）：

- **不新建第二套消息入口**；不破坏普通聊天气泡、`StepRunJobBody`、`run_next`、`jobRunLive`、SSE、`GET Job` 刷新恢复。
- `routing` 只存路由/策略；**执行过程与运行状态放 `run_view`**（`process_log` **不得**放进 `routing`）。
- 过程内容只能是**安全摘要**：不发原始工具参数/响应、DSML/XML、绝对本地路径、令牌、模型完整思维链。
- `kind`（read/edit/command/tool/thinking/system）**由后端判定**，前端不按工具名猜。
- 所有过程事件都要 `entry_id` + `sequence` + 稳定 `call_id`，前端按此去重（SSE 重连/轮询/刷新不重复）。

前端已由用户自行完成（`src/services/sse.js` 的 `normalizeAgentProcessEvent` / `mergeAgentProcessLog` / `normalizeJobRunView` /
`buildAgentMessageView` / `createAgentFinalBuffer` / `shouldEnterAgentMode`、`chat.js` 的最终回答缓冲、
`AgentJobBubble.jsx` 的 `AgentExecutionLog`、`Main.jsx` 分流），并有以下冒烟：`electron/agent-process-log.smoke.cjs`。
后端只需保证字段契约与它们一致（见 §4）。

## 2. 已完成（都在工作区，未提交）

### 2.1 共享契约（`packages/contracts`）
- `src/lumi_contracts/events/process.py`（新）：`ProcessLogEntry`（字段 `id/entry_id/kind/title/summary/detail/status/step_id/call_id/tool_name/sequence/occurred_at/job_id`）、
  `ProcessKind`、`ProcessStatus`、`derive_kind()`、`sanitize_process_text()`（剥绝对路径/凭据，DSML/原始载荷整条→`[已隐藏]`）、
  `merge_process_log(existing, incoming, limit=200)`（去重键：`entry_id` > `call:<call_id>` > `seq:<job>:<sequence>`）。
- `persistence/run_view.py`：`JobRunView.process_log` + `with_process_log()`（去重 + 200 滚动窗口）。
- `events/process.py::from_event()` 已能读嵌套 `step{tool,title,id,status,runtime_status}`、`display{working,completed}`、`result_summary`；
  **绝不读** `arguments/params/result/output/prompt/reasoning`。
- `projections/model.py`：列表型 payload（`sections`/`matches`/`entries`）统一行式渲染（`[路径 · 位置]` + 摘要），非列表型仍 JSON。
- TS 导出已重生成：`packages/contracts/ts/lumi-contracts.d.ts`（含 `ProcessLogEntry`/`JobRunView.process_log`）。

### 2.2 SSE 出口（唯一补齐点，非新入口）
- `app/contracts/events.py::SseEventEncoder.frame`：对 `_PROCESS_EVENT_TYPES = {process, step, step_started, step_completed, plan_ready, tool, tool_started, tool_completed, approval_required, approval_resolved}`
  补齐 `entry_id/kind/title/summary/detail/safe_detail/status/step_id/call_id/tool_name/sequence/occurred_at`；**`delta`/`done` 帧完全不改**。

### 2.3 持久化与恢复
- `Job.process_log`（`app/agents/orchestration/models.py`，落 Job 快照，**不在 routing**）。
- `app/contracts/process_log.py`（新）：`process_log_from_job()` / `merge_job_process_log()` / `process_log_payload()` / `persist_process_log()`；
  `entry_id` 约定 `plan:r<plan_revision>`（seq 0）、`step:<step_id>`（seq = index+1）；文案复用 `app/agents/orchestration/presentation.py`；全部过 `sanitize_process_text`。
- `app/agents/orchestration/step_run_service.py::save_state` 保存前 `persist_process_log(job)`。
- `GET /agents/jobs/{id}` 与 cancel 返回 `run_view.process_log`（持久化 ∪ 现算）；
  ⚠️ `list_agent_jobs` 主动剔除 `process_log`（避免轮询放大）——这是待用户确认的一个判断点。

### 2.4 发射点语义字段（内核层）
- `packages/execution/src/lumi_execution/step_engine.py`：`step_started` / `process` / `tool_completed` / `waiting_approval` / `step_completed` / `_tool_started`
  增加 `entry_id`（`step:<id>`、`call:<call_id>`、`process:<step>:<seq>`）、`kind`、`title`、sanitized `summary`、`status`；`result_summary` 语义不变（**不是**最终结果）。
- `packages/orchestration/src/lumi_orch/protocol.py`：`process` / `tool` 事件同上。
- `app/services/orchestrator.py`：`_process_fields()` / `_step_frame_fields()`；M1 atomic-read process 帧、`plan_ready`（`entry_id=plan:r<rev>`）、
  各处 `{"type":"step"}` 帧（原来 `entry_id=seq:job:n`、summary 为空 → 现在 `step:<id>` + 非空 title/summary）、协议透传帧。
- 依赖：`packages/{execution,orchestration}/pyproject.toml` 加 `lumi-contracts>=0.1.0`，`uv.lock` +4 行。

### 2.5 测试（新增）
- `tests/agents/test_agent_process_bubble_contract.py`（8，我实跑通过）
- `tests/agents/test_agent_process_log_persistence.py`（9）
- `tests/contracts/test_process_event_semantics.py`（10，我实跑通过）
- 其余相关：`tests/contracts/test_stream_event_contract.py`、`tests/orchestration/test_run_view_contract.py`、`tests/contracts/test_projection_outlets.py`、`tests/platform/test_model_projection_lists.py`、`tests/contracts/test_contracts_ts_export.py`

## 3. 唯一剩余的代码活：遗留项 (a) —— 实时与刷新文案一致

**现状**：实时帧的 `title/summary` 来自内核（步骤声明的 instruction/title）；刷新后 `process_log_from_job()` 用的是
`presentation.intent_text/working_text/completed_text`。两者 `entry_id` 相同（同一行，不会重复），但**措辞可能不同**。

**做法（用户已选定 a 方案）**：
1. 在 **app 层**（`app/agents/orchestration/step_run_service.py` 转发/构造 `step_started` 与 office `step` 帧处）注入 presentation 文案：
   - pending/未开始 → `presentation.intent_text(node)`
   - running → `presentation.working_text(node)`
   - completed/failed → `presentation.completed_text(node, result)` / `failed_text(node, error)`
   使实时帧与 `process_log_from_job()` **用同一套函数**。
2. **不要**在内核里 import `app.*`（内核不得依赖应用层）；也**不要**两层重复同一句话（内核已有的声明文案被 app 层按 step 解析后覆盖即可）。
3. 回归**只加 1 条**（用户要求用例不要太多）：同一 step 的实时帧与 `process_log_from_job()` 产出的 `title/summary` 相等且 `entry_id` 相同。

## 4. 字段契约（前端依赖，别改）

SSE 过程帧（扁平，scalar 全 snake_case）：
```
type entry_id kind title summary detail safe_detail status step_id call_id tool_name sequence occurred_at job_id version seq
```
`run_view.process_log`：`ProcessLogEntry` 列表（pydantic dump = snake_case，字段见 §2.1）。
`entry_id` 约定：`plan:r<rev>` / `step:<step_id>` / `call:<call_id>` / `process:<step_id>:<seq>` / `process:chat:<n>` / `process:atomic_read:<n>`。
终态语义（**不要改**）：`task_completed.final_answer` 是最终结果；`done` 每任务只发一次；失败由 `task_failed` 携带错误后由 `done` 收敛；
`step_completed.result_summary` 只是步骤结果，**不得**当成最终总结。

## 5. 已知挂起（重要，别踩坑）

`tests/orchestration/test_logical_plan.py::test_logical_plan_replan_replaces_only_unfinished_tail`（6 个里的第 5 个）
在**本机环境**会挂住（>90s 无进展）。已做的背靠背对比（同环境、同命令）：

| 条件 | 结果 |
|---|---|
| 带本次 3 个内核文件改动 | HUNG（90s 杀掉） |
| 这 3 个文件 `git stash` 后 | HUNG（90s 杀掉） |

→ **不是这 3 个内核文件引起的**。注意 stash 只覆盖了 `packages/execution/.../step_engine.py`、`packages/orchestration/.../protocol.py`、`app/services/orchestrator.py`；
若要 100% 排除本次会话全部改动，用同样方法把 `app/contracts/process_log.py` / `step_run_service.py` / `app/api/v1/agents.py` 一起 stash 再对比。
另：该用例单跑要 16s 级等待/轮询，环境负载高时可能只是慢。**用户明确要求：不要跑全量；单测卡住就终止。**
历史坑：一个从 13:52 起挂死的旧 pytest（修复前的客户端审批轮询死循环，已加 `max_polls` 上限修掉）曾烧 4057s CPU 并干扰所有测试运行——如再看到长时间高 CPU 的 python 进程，先查再跑。

## 6. 运行与验证规范（务必遵守）

```powershell
# 环境
$env:TEMP="E:\pythonpycharm\lumi_backend\.ptmp"; cd E:\pythonpycharm\lumi_backend
$py=".venv\Scripts\python.exe"

# 有界运行单个/少量用例（卡住自动杀，绝不死等）
$p=Start-Process -FilePath $py -ArgumentList @('-m','pytest','<nodeid>','-q','-p','no:cacheprovider') -PassThru -NoNewWindow `
   -RedirectStandardOutput .ptmp\out.txt -RedirectStandardError .ptmp\out.err
if(-not $p.WaitForExit(90000)){ $p.Kill(); "HUNG" }

# lint（只查改动文件）
& $py -m ruff check <files> --no-cache
```
- **禁止跑全量**（用户要求）。只跑聚焦集：`tests/contracts/test_process_event_semantics.py`、`tests/agents/test_agent_process_log_persistence.py`、
  `tests/agents/test_agent_process_bubble_contract.py`、`tests/contracts/test_stream_event_contract.py` + 你新加的 1 条。
- 如需对比"有无改动"，把 `git stash push` 与 `git stash pop` 放在**同一条命令**里，并在 run 外面套 `WaitForExit(90000)`+`Kill`，
  否则命令被中断会把仓库留在 stash 状态。
- 基线（供参考，非本次必须复现）：第一个子任务后 `858 passed`；子任务自报带内核改动 `867 passed`；两者都未在"本次全部改动 + 干净环境"下复验过。

## 7. 待用户决策

1. `list_agent_jobs` 是否也返回 `process_log`（现为剔除）。
2. 那个挂起用例是否给它内部超时/标记（不建议用改测试来掩盖）。
3. 可选加固：出口顺带 sanitize `run_view.steps[].description/result_summary`（既有计划通道，本次范围外）。

## 8. 工作区状态

未提交（`git stash list` 为空，无残留进程）。改动文件见 §2；`docs/handoff-agent-process-bubble.md` 为本文件。
建议：完成 §3 后由用户 `git add -A && git commit`。
