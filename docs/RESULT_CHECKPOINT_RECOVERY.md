# 结果存储、检查点与恢复（整合版）落地说明

本文对应《结果存储、检查点与恢复方案（整合版）》，记录**后端已落地的实现入口**、
接入开关、接口与验收对照。方案里的"方案 1（合同/中间层 contract）"与"方案 2（多模态架构）"
分别是本仓库的 `packages/contracts`（跨模块契约 + 事件协议）与内容抽象层；本方案本身
只负责"一切状态落在哪里、崩了之后凭什么恢复"。

## 一句话链路

```
Tool / Skill / LLM
  → ExecutionResult
  → ResultStore 保存结果，返回 result_ref（sha256 + schema_version + expires_at）
  → Step Checkpoint 记录步骤状态（落盘在检查点键，不进 Job 快照）
  → Redis Job State / JobRunView（只留摘要 + 引用，有界）
  → SSE 实时事件（方案 1 的事件协议） + GET /jobs/{id} 快照恢复
```

原则：**SSE 只负责实时展示，不作为唯一事实源**；热状态只存摘要与引用，完整结果只在
引用可达的地方。

## 接入开关（默认全关，关掉即回到旧路径）

| 开关 | 作用 | 关闭时 |
| --- | --- | --- |
| `RESULT_STORE_V2` | 结果统一进 `ResultStore`（分层 + 过期 + 版本化解析） | 保持既有 `{id, sha256}` 引用路径 |
| `STEP_CHECKPOINT_V2` | 每步写检查点，完成事件在检查点落盘之后 | 不写检查点，行为与改造前一致 |
| `EFFECT_JOURNAL_TYPED_V2` | 副作用日志记 `effect_type` / `effect_key`（在途核对） | 只记旧的意图字段 |
| `JOB_PROJECTION_ENABLED` | `job_runs` / `job_steps` 异步 DB 投影 | 不写 DB（Redis 路径仍然完整） |

## 代码入口

### 契约（`lumi_contracts.persistence`，backend-neutral）

| 模块 | 内容 |
| --- | --- |
| `result_store.py` | `ResultRef`（id/sha256/storage_kind/schema_version/expires_at…）、`TieringPolicy` 分层决策、`LoadBudget` + `bound_body` 按预算裁剪、`RESULT_REF_*` 稳定错误码 |
| `checkpoint.py` | `StepCheckpointState` 状态机（`ALLOWED_TRANSITIONS`）、`StepCheckpoint` 落盘记录、`is_regression` 单调性、`assert_emit_after_persist` 时序校验、`checkpoint_is_stale` |
| `recovery_plan.py` | `plan_resume`（§5 第 3–7 步判定链）、`RecoveryDecision`、`settle_reconciled_step` |
| `effect_recovery.py` | 副作用三态 → 恢复动作（`pending` 为主口径，`intent` 为等价别名） |
| `run_view.py` | `JobRunView` 有界快照（200 条 / 单条 2KB / 256KB 收缩） |

### 应用层

| 模块 | 内容 |
| --- | --- |
| `app/services/result_store.py` | 统一读写入口；`RedisKvPort` / `LocalBlobPort` / `S3BlobPort`（MinIO/OSS，`boto3` 惰性导入）；`save_result` / `resolve_result` 兼容入口 |
| `app/services/step_checkpoint.py` | `StepCheckpointCoordinator`（唯一写入点）、Redis hash 落盘、`multiagent:step_checkpoints:{job_id}`、`confirm_persisted_for_emit` 完成事件闸门 |
| `app/services/job_projection.py` | `JobProjector` 攒批 + 幂等 upsert；DB 失败保留待补写、不阻塞执行 |
| `app/agents/orchestration/job_recovery_service.py` | `JobRecoveryService.plan/plan_for_job`、`EffectReconciler` 在途核对端口、按引用 + 预算重载依赖 |
| `app/agents/orchestration/execution/lineage.py` | `persist_result_ref` / `resolve_result_ref`（`RESULT_STORE_V2` 打开时委托给 ResultStore） |
| `app/agents/orchestration/step_run_service.py` | `save_state` 落 Job 后写检查点；出口处核对完成事件时序 |
| `app/services/job_snapshot_store.py` | 运行视图快照的**唯一**写入路径（未改动其契约） |
| `app/services/process_log_archive.py` | 过程日志溢出归档（`ARCHIVE_CONTENT_V2`） |

### 数据与迁移

* `alembic/versions/0015_result_checkpoint_projection.py`
  * `effect_journal` 增加 `step_id` / `attempt` / `effect_type` / `effect_key` / `result_ref`，
    状态约束放宽为 `('intent','pending','confirmed','uncertain')`；
  * 新建 `job_runs`（任务控制面投影）与 `job_steps`（步骤检查点投影，唯一键
    `(job_id, step_id, attempt)`）。
* 两张投影表**不是实时事实源**：由 `JobProjector` 批量幂等 upsert，DB 写失败不影响执行。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/agents/jobs/{job_id}` | 快照恢复（`run_view` / `process_log` / `log_archive_ref` / `artifact_refs` / `checkpoint_summary`） |
| GET | `/api/v1/agents/jobs/{job_id}/steps` | **新增**：分页步骤检查点（`display_summary` + 最小引用 + `entry_id=step:<id>`） |
| GET | `/api/v1/agents/jobs/{job_id}/results/{result_id}` | **新增**：按权限解析 `result_ref`（正文按需加载；分页/预算前端传、后端执行） |
| GET | `/api/v1/agents/jobs/{job_id}/recovery` | **新增**：只读恢复核对报告（不触发重排/重跑） |
| GET | `/api/v1/agents/jobs/{job_id}/stream` | SSE 实时事件 |
| POST | `/api/v1/agents/jobs/{job_id}/approve` `resume` `cancel` | 审批 / 恢复 / 取消 |

### `/results/{result_id}` 的错误语义（前端据此分派）

| 情况 | 状态码 | 响应 |
| --- | --- | --- |
| 正常 | 200 | `data` = 结果正文 + `result_id` / `sha256` / `schema_name` / `schema_version` / `size` / `storage_kind` / `expires_at` / `truncated` / `degraded` |
| 结果不存在 | 404 | 统一错误体，`data.error_code=RESULT_REF_UNAVAILABLE` |
| 引用已过期 | **410** | 统一错误体，`data.error_code=RESULT_REF_EXPIRED`（终局，不可重试；**不返回空成功**） |
| 完整性校验失败 | 404 | 统一错误体，`data.error_code=RESULT_REF_INTEGRITY_FAILED`（拒绝交付正文） |
| 不是自己的任务/结果 | 403 / 404 | 归属先于结果：任务不可见即 404，任务可见但结果越权即 403 |
| `schema_version` 不兼容 | 200 | `degraded=true` + `degradation=validation.schema_mismatch` + 原始内容（**不用当前版本硬解析历史数据**） |

> 未上线的接口返回的是框架默认 `{"detail": "Not Found"}`；只要出现统一错误体就说明
> 接口在线（这一区分是前端 `resultRefs.js` 判定 `RESULT_ENDPOINT_UNAVAILABLE` 的依据）。

分页与预算参数**全部由前端传入、后端执行**：`max_chars`（字符预算）、`page_size` /
`page_offset`（列表字段分页）、`fields`（逗号分隔的字段白名单，只读指定字段）。

`/recovery` 返回 `decision`（`RESUME` / `NEEDS_HUMAN` / `ALREADY_SETTLED`）、
`skip_step_ids`（副作用已确认，跳过不重跑）、`reschedule_step_ids`（明确未执行）、
`reconcile_step_ids`（在途，先核对实际状态）、`paused_step_ids`（交人工）、
`stale_step_ids`（检查点陈旧，以 Job 状态为准）。

### 过程状态词表（前端时间线消费）

`ProcessStatus` = `running | completed | failed | pending | uncertain | cancelled | expired`，
另外**未登记但有值**的状态原样透传（前向兼容）。关键点：`uncertain`（副作用可能在途、
等人工确认）在**实时帧与刷新快照两条路径**上都原样保留——它既不能被兜底成 `completed`
（那样"在途取消"这类验收场景在界面上会彻底消失），也不能被压成 `failed`（会被当成
明确失败而重跑）。

## 三条快照硬边界（§3）

| 规则 | 阈值 | 落点 |
| --- | --- | --- |
| 过程日志有界 | 最近 200 条 | `PROCESS_LOG_MAX_ENTRIES` + `roll_process_log` |
| 单条日志限长 | `summary ≤ 2KB` | `PROCESS_LOG_ENTRY_MAX_BYTES` + `clip_utf8` |
| 快照限重 | `> 256KB` 告警 + 收缩 | `SNAPSHOT_MAX_BYTES` + `JobRunView.bounded_snapshot()` |

超窗日志转存为 `process_log_archive` 产物，快照只留 `log_archive_ref` + 条数。

## Effect Journal 三态（§4）

| 状态 | 含义 | 恢复动作 |
| --- | --- | --- |
| `confirmed` | 副作用已确认完成 | 跳过，不重跑 |
| `pending`（旧记录写作 `intent`） | 请求已发出、未确认 | 可核对 → 先查实际状态；不可核对 → 暂停等人工 |
| `uncertain` | 无法确认是否发生 | 暂停，绝不自动重跑 |

幂等键 `effect_key`（目标路径 hash / 消息 ID）+ `effect_type` 决定"怎么核对"；
插件卸载/崩溃熔断后其已发出的副作用同样以 Journal 为准，不因插件不在了就假设没发生。

## 验收对照（§9）

| 场景 | 落点 |
| --- | --- |
| 普通短回答不被错误上传 Blob | `tests/contracts/test_result_store.py::test_short_result_stays_in_kv_and_never_touches_blob` |
| 几十页正文明细不进快照、可分页/按引用读取 | `test_large_result_goes_to_blob_and_is_readable_by_reference` + `test_budgeted_load_*` |
| 引用 hash 错误被拒绝 | `test_integrity_failure_is_rejected` |
| 结果引用过期明确报错 | `test_expired_reference_raises_explicit_error` |
| 旧 result_ref 用旧 schema 解析 | `test_history_is_parsed_with_the_stored_schema_version` |
| 第 N 步完成后崩溃可恢复 | `tests/orchestration/test_job_recovery_flow.py::test_completion_event_arrives_after_state_and_checkpoint_are_persisted` |
| 审批后崩溃仍为 waiting_approval | `tests/orchestration/test_step_checkpoint.py::test_approval_then_crash_keeps_waiting_approval_state` |
| 在途取消 → uncertain，恢复时判定而非重跑 | `test_cancel_while_effect_in_flight_marks_uncertain` + `test_recovery_service_reconciles_pending_effect_when_verifiable` |
| 500 步长任务快照有界（检查点独立落盘） | `test_checkpoint_is_isolated_from_job_snapshot_and_bounded` |
| 日志不可用 fail-closed | `test_recovery_service_fails_closed_when_journal_unavailable` |
| DB 写失败不阻塞执行、可补写 | `tests/orchestration/test_job_projection.py::test_db_failure_keeps_rows_for_later_and_never_raises` |
| 引用接口：越权 404 / 过期 410 + 明确错误码 / 分页预算 | `tests/contracts/test_result_reference_api.py` |
| `uncertain` 在实时与刷新两条路径都不被吞掉 | `test_uncertain_status_survives_both_live_and_refresh_paths` + `test_process_event_semantics.py` |
| `/steps` 分页 + `display_summary` + `entry_id` 去重键 | `test_steps_endpoint_paginates_and_exposes_display_summary` |

## 前端 ↔ 后端对齐（Electron 客户端 `src/services/resultRefs.js`）

参数名与错误码两侧**逐字对应**（前端 `result:fetch` / `job:steps` 主进程通道 → 后端路由）：

| 前端传参 | 后端 Query | 语义 |
| --- | --- | --- |
| `mode=full\|summary` | `mode` | `summary` 只读摘要（不返回正文） |
| `offset` / `limit` | `offset` / `limit` | 列表字段分页（服务端执行） |
| `max_chars` | `max_chars` | 字符预算（超出截断 + `truncated=true`） |
| `fields=a,b` | `fields` | 字段白名单（逗号分隔） |
| `include_results` | 忽略 | 步骤接口只给最小引用，正文永远按引用另取 |

响应里额外回传 `offset` / `limit` / `total` / `degraded` / `degradation` / `note`，
前端**不需要**从 `schema_version` 反推要不要降级。

错误码别名（前端展示码保持稳定，后端契约码是权威）：

| 后端码 | 前端展示码 | 前端语义 |
| --- | --- | --- |
| `RESULT_REF_EXPIRED`（HTTP 410） | 同名 | 终局，不可重试 |
| `RESULT_REF_UNAVAILABLE`（HTTP 404） | `RESULT_NOT_FOUND` | 终局，**不是**"接口未上线" |
| `RESULT_REF_INTEGRITY_FAILED`（HTTP 404） | `RESULT_HASH_MISMATCH` | 拒绝展示 + 可降级为原始产物 |
| `PERMISSION_DENIED`（HTTP 401/403） | 同名 | 终局 |
| `{"detail":"Not Found"}`（框架默认） | `RESULT_ENDPOINT_UNAVAILABLE` | 接口未上线，降级下载原件 |

步骤记录（`/steps`）每行同时给 `display_summary`（展示摘要）与 `entry_id=step:<id>`
（与过程日志**同一个**去重键），并有 `effect_status` / `attempt` / `error_code` /
`result_ref`；快照 `run_view.steps[]` 也带 `display_summary` / `attempt` / `effect_status` /
`result_ref`，因此刷新路径与 `/steps` 分页路径渲染同一行。

`step_completed` 事件给 `result_ref`（对象引用最低要求 `id` + `sha256`）、
`display_summary`、`output_summary`、`artifact_refs`；`uncertain` 状态如实下发。

前端冒烟：`node electron/result-ref.smoke.cjs`（含后端契约码对齐断言）、
`LUMI_CONTRACTS_DTS=<backend>/packages/contracts/ts/lumi-contracts.d.ts node electron/capability-contract.smoke.cjs`
（逐值比对后端 d.ts）。

## 未在本轮改动范围内的部分

* 前端"事件 + 快照双来源"、归档日志按需加载（§6）属于前端改造；
* Blob 后端的**生产部署**（MinIO/OSS 凭据与生命周期策略）需要部署侧配置，代码侧只提供
  `RESULT_STORE_BLOB_*` 与 `S3BlobPort`（缺 `boto3`/未配置 bucket 时按不可用处理，
  并按 `RESULT_STORE_BLOB_FALLBACK_LOCAL` 决定回退本地还是失败）。
