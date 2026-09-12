# 流式事件协议（标准事件信封 + 双投影）

> 状态：**第一阶段已落地**（后端内部只生成一套标准事件，外层可投影成旧版或新版 SSE）。
> 落点：`packages/contracts/src/lumi_contracts/events/envelope.py`（契约）、
> `app/contracts/event_adapter.py`（旧字段 → 标准载荷的唯一适配器）、
> `app/contracts/events.py`（SSE 投影）、`app/contracts/ui_projection.py`
> （`ExecutionResult` → UI 事件）。

## 1. 三条硬约束

1. **不新增第二套事件系统**：只扩展既有 `StreamEvent` / `ProcessLogEntry` /
   `ExecutionResult` / `JobRunView`；没有第二个事件总线、第二个 SSE 出口。
2. **`thought_delta` 不传原始思维链**：过程类事件只允许后端生成的**安全摘要**。
   原始推理、工具参数、完整工具结果在**两处**被结构性剔除（适配器白名单 +
   契约工厂 `strip_unsafe_payload`），不是"靠约定不写"。
3. **字段只有一套权威定义**：`event_id` / `version` / `seq` / `type` / `trace_id` /
   `conversation_id` / `job_id` / `occurred_at` / `payload`。沿用既有 `job_id`，
   **不新增 `job_run_id`**；`data` 只作为旧版传输字段，由投影层生成。

## 2. 标准信封

```json
{
  "event_id": "evt_6f8b93bff0c3fbe6",
  "version": 2,
  "seq": 7,
  "type": "step_completed",
  "trace_id": "",
  "conversation_id": "c1",
  "job_id": "j1",
  "occurred_at": "2026-09-12T04:00:50.497450+00:00",
  "payload": { }
}
```

* `seq`：**每条流内单调递增**，用于发现丢帧；
* `event_id`：去重键。事件带稳定身份（`entry_id` / `call_id` / `step_id`）时是
  **内容哈希**，因此 SSE 重连、快照恢复、重复发射的同一逻辑事件得到同一个 id；
  否则是随机唯一 id；
* `dedup_key` 规则：`event:{event_id}`，没有 event_id 时退化为 `seq:{job_id}:{seq}`
  （与旧过程日志同规则）；
* 时间统一 ISO 字符串（`occurred_at`）。

## 3. 事件类型收敛表

| 旧事件 | 标准事件 |
|---|---|
| `delta` / `text` / `message` | `text_delta` |
| `process` / `thinking` | `process` |
| `tool_started` / `step_started` / `tool` | `step_started` |
| `tool_completed` / `step_completed` | `step_completed` |
| `step`（双态） | `status=running` → `step_started`；`completed/failed/...` → `step_completed` |
| `artifact` / `artifact_created` | `artifact_created` |
| `view_updated` | `view_updated` |
| `approval_required` | `approval_required` |
| `approval_resolved` | `approval_resolved` |
| `done` / `task_completed` | `control(state=completed)` **+ 兼容伴随帧 `done`** |
| `task_failed` | `control(state=failed)` **+ 兼容伴随帧 `error`** |
| `error` | `error` |

未列入收敛表的既有类型（`job` / `task_router` / `plan_ready` / `plan_delta` /
`waiting_next` / `capability_*` / `operation_*` / `plugin_health_changed`）
**原样透传**（前端契约要求未知类型不得白屏），载荷仍走安全白名单。

## 4. 各类型 Payload 字段

| 类型 | 字段 |
|---|---|
| `text_delta` | `content`、`format`（`markdown` \| `plain`）、`message_id` |
| `process` | `entry_id`、`kind`、`title`、`summary`、`detail`、`status`、`step_id`、`call_id`、`tool_name`、`sequence`（即 `ProcessLogEntry` 安全字段） |
| `step_started` | `step_id`、`step_type`、`name`、`display_summary`、`tool_name` |
| `step_completed` | `step_id`、`status`、`duration_ms`、`output_summary`、`error_code`、`result_ref`、`artifact_refs` |
| `artifact_created` | `artifact_id`、`type`、`filename`、`mime_type`、`size_bytes`、`expires_at` |
| `view_updated` | `view_id`、`view_type`、`plugin_id`、`plugin_version`、`action`、`schema_version`、`title`、`data`、`data_ref` |
| `approval_required` | `request_id`、`step_id`、`capability`、`action`、`target`、`risk_level`、`preview_ref`、`expires_at` |
| `approval_resolved` | `request_id`、`step_id`、`approved`、`resolved_by`、`reason`、`decided_at` |
| `control` | `state`、`reason_code`、`reason`、`next_action` |
| `error` | `code`、`message`、`retryable`、`step_id`、`suggested_action` |

约定：

* `step_started` **不传完整 inputs**，只给脱敏展示摘要；
* `step_completed` **不传完整结果**，只给 `result_ref` + `output_summary` + `artifact_refs`；
* `artifact_created` **不传下载地址/令牌**：前端点击后再走受权限保护的下载接口；
* `view_updated` 第一阶段只支持**声明式视图**，`view_type` 限白名单
  （`table` / `chart` / `diff` / `timeline` / `file_preview` / `form`），
  非白名单值被收敛为空；不开放任意 iframe / HTML / JS；
* `text_delta.content` **不做展示级截断**（正文不能被事件层改字），只有失控增量护栏。

## 5. 旧字段到新事件的适配

* 唯一适配器：`app/contracts/event_adapter.py::canonical_events()`；调用方（SSE 出口）
  只需调用一次，业务代码不维护两套逻辑；
* 过程/步骤/审批字段全部经 `ProcessLogEntry.from_event()` 安全提取（去绝对路径、
  去凭据、去 DSML/原始载荷）；
* 白的进、黑的挡：`ALLOWED_PAYLOAD_KEYS` 白名单 + `FORBIDDEN_PAYLOAD_KEYS`
  危险键兜底；不透明容器（`data` / `result_ref` / `artifact_refs`）只清危险键 + 限体积
  （400 KB）；
* 反向（标准 → 旧版）不需要适配器：旧投影就是**原始扁平事件 + `version`/`seq`**，
  与历史输出逐字节一致。

## 6. 两种投影与切换

| 投影 | 触发 | 形状 |
|---|---|---|
| `legacy`（默认） | `STREAM_EVENT_PROTOCOL=legacy` | 扁平字段 + `version` / `seq`（前端当前消费） |
| `canonical` | `STREAM_EVENT_PROTOCOL=canonical` | 标准信封（`payload` 唯一负载） |

编码器 API：`SseEventEncoder.frame()`（旧投影，不变）、`canonical_frames()`
（新投影，1..2 帧）、`frames()` / `encode_all()`（按协议自动选择）。
**四个既有 SSE 出口已全部改用 `encode_all()`**（`/chat/stream`、`run_next`、
会话多端推送、语音通话），因此 `STREAM_EVENT_PROTOCOL=canonical` 一处生效、
默认 `legacy` 时输出与历史逐字节一致；切换只影响投影，不影响业务逻辑与持久化。

## 7. 持久化与恢复

SSE 只负责实时展示，不是唯一事实来源。`JobRunView`（`lumi.job_run_view@1`）继续保存：

* `process_log`：过程日志（安全摘要 + 去重键，≤200 条，按 `entry_id` 合并）；
* `final_answer`：最终回答（≤20000 字符）；
* `steps[].result_ref` / `duration_ms` / `error_code`：步骤结果引用；
* `last_seq`：事件水位（只增不减，前端据此判断快照是否已覆盖自己的 `lastSeq`）；
* `artifact_refs`：产物引用（按 `artifact_id` 去重合并，≤50）；
* `views`：声明式视图引用（`view_id` + `data_ref`，数据按需再取）。

刷新恢复顺序：`GET /agents/jobs/{id}` → `JobRunView` → 过程日志 + 最终回答 +
产物/视图引用 + `last_seq`；需要增量时再走 §9.4 的 `events?after_seq=` 补拉。

## 8. 落地状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1 | 冻结 Contracts SDK 事件模型（信封 / 载荷 / 版本 / 去重 / 收敛表） | ✅ |
| 2 | 内部标准事件工厂 + 旧字段适配器 | ✅ |
| 3 | `ExecutionResult` → UI 事件投影（结果类事件已走该链路：`node.result → ExecutionResult → UI Projection → Event`） | ✅ |
| 4 | Orchestrator 出口接线（`text_delta` / `process` / `step_*` / `approval_required` / `approval_resolved` / `artifact_created` / `view_updated` / `control` / `error`） | ✅ |
| 5 | 保留旧 SSE 适配输出（默认 `legacy`，前端零改动） | ✅ |
| 6-9 | 前端 `StreamConsumer`、切 `canonical`、写入/审批/沙箱、Artifact/View Plugin | 前端已完成主体；后端联调项见 §9 |

回归：`tests/test_stream_event_envelope.py`（信封字段 / 收敛表 / 去重 / 脱敏 /
终态状态 / 路由元数据）、`tests/test_artifact_view_replay.py`（产物签名与越权 /
结果归一化链 / 视图白名单 / 审批标识与决议 / 终态透传 / 按 seq 补拉）、既有
`tests/test_stream_event_contract.py`（旧投影逐字不变）。

## 9. 联调检查清单（前端四项，后端已就绪）

### 9.1 `artifact_created` + 受权限保护下载

| 项 | 值 |
|---|---|
| 触发方式 | 任务的某个节点完成且结果里有产物（`node.result.outputs[].name`）。同一产物只下发一次（断线重连/轮询不叠加） |
| 事件 | `artifact_created` |
| 期望字段（payload） | `artifact_id`、`filename`、`mime_type`、`size_bytes`、`type`（扩展名）、`expires_at`（ISO，默认 7 天） |
| 下载接口 | `GET /api/v1/artifacts/{artifact_id}/download`（需登录；归属 + 有效期 + 路径越权三重校验） |
| 元数据接口 | `GET /api/v1/artifacts/{artifact_id}` → `{artifact_id, filename, mime_type, size_bytes, type, expires_at, download_path}` |
| 刷新恢复 | `GET /api/v1/agents/jobs/{job_id}` → `data.run_view.artifact_refs[]`（**已过滤过期/已清理**，形状同事件 payload） |
| 前端断言位置 | `ArtifactCard` 的 `onDownload` 注入点：**渲染阶段不发请求**，点击才请求下载接口 |
| 降级 | 404（不存在/越权/过期）→ 显示"产物已过期，请重新生成"；不要重试下载 |

### 9.2 `approval_required` 的 node/step 标识 + `approval_resolved`

| 项 | 值 |
|---|---|
| 触发方式 | 节点进入 HITL 审批门（`node.metadata.awaiting_approval`）；同一节点只下发一次 |
| 事件 | `approval_required` → 用户做决定后同一节点再下发 `approval_resolved` |
| 期望字段（`approval_required`） | `request_id`、**`node_id`**、`step_id`、`capability`、`action`、`target`、`risk_level`、`preview_ref`、`expires_at` |
| 期望字段（`approval_resolved`） | 同上的 `request_id`/`node_id`/`step_id` + `approved`(bool) + `resolved_by` + `reason` + `decided_at` |
| 审批接口 | `POST /api/v1/agents/jobs/{job_id}/approve`，body `{"node_id": <payload.node_id>, "approved": true/false}` |
| 决议来源 | 读既有审批写入的结果（批准：`awaiting_approval` 移除 + `confirmed_tool_calls` 写入；拒绝：节点 `skipped`；超时：`APPROVAL_TIMEOUT`），**不重新判断权限** |
| 前端断言位置 | 卡片：`node_id` 用 payload 的 `node_id`（`step_id \|\| request_id` 可留作兜底）；收到 `approval_resolved` 时按 `approved` 收起卡片（拒绝时展示 `reason`） |
| 说明 | `step_id` 是展示步骤 id，通常与 `node_id` 相同但不保证；`request_id` 仅用于前端去重与展示 |

### 9.3 `view_updated`（声明式视图）

| 项 | 值 |
|---|---|
| 触发方式 | ① 可预览产物生成时（csv/tsv/xlsx → `table`；txt/md/log/json/xml/yaml/html/docx/pptx → `file_preview`）；② 任务结束时的步骤时间线（`timeline`，`view_id=view:timeline`） |
| 期望字段（payload） | `view_id`、`view_type`、`plugin_id`（本期 `lumi.core`）、`plugin_version`、`action`（`upsert`）、`schema_version`、`title`、`data`、`data_ref` |
| 白名单 | `table` / `chart` / `diff` / `timeline` / `file_preview` / `form`；**非白名单一律置空**，前端显示"暂不支持此展示类型" |
| 不产出的视图 | 需要完整 Diff 正文的 `diff`（服务端只留统计与引用）、需要插件数据的 `chart`/`form`（等 View Plugin 阶段） |
| 数据有界 | 单视图 data ≤ 400KB，超限只给 `data_ref`（此时前端按引用按需取） |
| 刷新恢复 | `GET /api/v1/agents/jobs/{job_id}` → `data.run_view.views[]` |
| 前端断言位置 | `ViewContainer`：未知 `view_type` 不白屏；`data` 为空时用 `data_ref` 或显示降级文案 |

### 9.4 断线续传（按 `seq` 补拉）

| 项 | 值 |
|---|---|
| 触发方式 | 断线重连 / 页面刷新后，发现 `run_view.last_seq` 大于本地 `lastSeq` |
| 接口 | `GET /api/v1/agents/jobs/{job_id}/events?after_seq=<本地 lastSeq>&limit=500` |
| 返回体 | `{job_id, protocol:"canonical", version:2, after_seq, last_seq, count, truncated, events:[标准信封…]}` |
| 一致性 | 日志里存的就是**实时流同一份帧**（同 `event_id` / `seq` / `type` / `payload`），因此可按 `event_id` 去重后直接累加 |
| 推荐恢复顺序 | ① `GET /agents/jobs/{id}` 取 `run_view`（`process_log` / `final_answer` / `artifact_refs` / `views` / `last_seq`）→ ② 若 `last_seq > 本地 lastSeq` 调 events 增量补拉 → ③ 用补拉结果覆盖/补齐状态机 |
| 前端断言位置 | `StreamConsumer`：`seq` 单调、`event_id` 去重、`truncated=true` 时继续补拉直到追平 |
| 降级 | 事件日志不可用（Redis 降级）或已过期（TTL 1 小时 / 每任务 800 条上限）时 `events: []` + `message` 提示 → 以 `JobRunView` 快照恢复为准，不要报错 |

> 备注：任务事件日志只记录**带 `job_id` 的帧**（普通闲聊不落日志，恢复靠消息历史）；
> `text_delta` 攒批写入，非正文帧（`process` / `step_*` / `artifact_created` /
> `view_updated` / `approval_*` / `control`）立即写入，保证过程与终态实时可补拉。

### 9.5 前端需要做的 / 需要确认的（后端侧已就绪）

1. **审批卡片两个标识**：优先用 `payload.node_id`（后端现在必下发）；**新增处理 `approval_resolved`**——否则卡片只能靠刷新或超时消失。
2. **`view_updated` 降级分支**：`view_type` 为空或不在白名单（`table`/`chart`/`diff`/`timeline`/`file_preview`/`form`）时显示"暂不支持此展示类型，可下载产物"；`data` 为空时用 `data_ref`。
3. **`artifact_created` 下载注入点**：`onDownload` → `GET /api/v1/artifacts/{artifact_id}/download`（需登录）；404 = 不存在/越权/已过期，提示重新生成，不要重试。卡片文件名/大小/过期时间直接取事件 payload。
4. **终态别只看 `done`**：`control.state` 现在给真实状态（`completed`/`failed`/`cancelled`/`interrupted`/`waiting_run`/`waiting_next`），失败/取消的任务不再显示成"已完成"。
5. **去重字段取决于协议**：`event_id` / `occurred_at` / `payload` **只在 canonical 投影里存在**；`legacy` 帧只有 `seq`。双识别期建议：有 `event_id` 用 `event_id`，没有就用 `seq`（两者都单调）。
6. **协议切换**：后端 `STREAM_EVENT_PROTOCOL=legacy|canonical`（默认 `legacy`）。前端 `StreamConsumer` 双识别就绪后把后端这一项切成 `canonical` 即可，切换不影响业务逻辑与持久化。
7. **补拉后的状态机**：先 `GET /agents/jobs/{id}` 取 `run_view`（含 `last_seq` / `artifact_refs` / `views` / `process_log` / `final_answer`），再用 `GET /agents/jobs/{id}/events?after_seq=` 追平；`truncated=true` 时继续拉。

> 结果类事件（产物/视图）现在走 `node.result → ExecutionResult → UI Projection → Stream Event`；
> 步骤状态帧仍来自步骤快照（它表达"步骤进度"，不是"结果"），两条都在同一套信封里。
