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
  "version": 1,
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

回归：`tests/contracts/test_stream_event_envelope.py`（信封字段 / 收敛表 / 去重 / 脱敏 /
终态状态 / 路由元数据）、`tests/contracts/test_artifact_view_replay.py`（产物签名与越权 /
结果归一化链 / 视图白名单 / 审批标识与决议 / 终态透传 / 按 seq 补拉）、既有
`tests/contracts/test_stream_event_contract.py`（旧投影逐字不变）。

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
| 返回体 | `{job_id, protocol:"canonical"|"legacy"（如实回报）, version:1, after_seq, last_seq, count, truncated, events:[标准信封…]}` |
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

## 10. 方案 2 整合版落地（统一错误 / 终态封印 / Schema 注册 / Fixture）

本节是「事件协议与前后端联调方案（整合版）」的后端交付记录。方案条目 ↔ 实现位置：

| 方案条目 | 后端实现 | 回归测试 |
|---|---|---|
| §1.3 Schema Registry + 未知事件策略 | `packages/contracts/.../events/registry.py`、`event_adapter._unknown_event_payload` | `tests/contracts/test_event_schema_registry.py` |
| §2.3 View 体积/嵌套上限（64KB / 10 层 / 1000 元素 → `data_ref`） | `envelope.ViewUpdatedPayload`、`envelope.bound_view_frame`（两种投影共用） | `tests/contracts/test_unified_error_model.py` |
| §3 统一错误模型 + 冻结码表 + ErrorTranslator | `packages/contracts/.../events/errors.py`、`envelope.ErrorPayload` / `ControlPayload` | `tests/contracts/test_unified_error_model.py` |
| §5.1 Artifact 短时下载 URL | `app/services/artifacts.py`、`app/api/v1/artifacts.py` | `tests/api/test_artifact_download_url.py` |
| §6.2 终态封印（后端闸门） | `app/services/job_event_seal.py`、接入 `chat.py` / `agents.py` / `job_event_log.record_frames` / `orchestrator.cancel_job` | `tests/contracts/test_event_terminal_seal.py` |
| §7.2–7.3 前端状态模型参考实现 | `app/contracts/stream_view_model.py` | `tests/contracts/test_stream_contract_fixtures.py` |
| §8 阶段 2/6/7 Fixture + 双协议一致 + 安全验收 | `docs/fixtures/stream-events/*.json` | `tests/contracts/test_stream_contract_fixtures.py` |

### 10.1 统一错误模型（`error` / `control` 载荷）

* 载荷字段只有：`code` / `category`(`transient|fatal|business|needs_human`) /
  `retryable` / `safe_message` / `detail_ref` / `step_id` / `suggested_action`；
  **原始异常文本、供应商响应、堆栈、工具参数不进载荷**（`message`/`error`/`stack`
  等键在构造时被结构性删除，公共实现 `SECRET_PAYLOAD_KEYS`）。
* 冻结错误码 12 个（方案 §3.2）：`TARGET_REQUIRED`、`DEPENDENCY_MISSING_WORKSPACE`、
  `CAPABILITY_UNAVAILABLE`、`PROVIDER_UNHEALTHY`、`PERMISSION_DENIED`、
  `TOOL_NOT_REGISTERED`、`APPROVAL_REQUIRED`、`SECURITY_BLOCKED`、
  `PLUGIN_RESOURCE_EXCEEDED`、`PLUGIN_UNINSTALLED`、`RESULT_REF_EXPIRED`、
  `SYSTEM_CANCELLED`；其余走域内码（`model.timeout` / `tool.failed` /
  `validation.schema_mismatch` / `plugin.crashed` / `resource.quota_exceeded` /
  `system.internal` …）。
* 仓库内既有错误码（`MCP_UNAVAILABLE` / `TIMEOUT` / `INVALID_ARGS` / `CLIENT_OFFLINE` …）
  通过 **唯一映射表** `LEGACY_CODE_ALIASES` 收敛：同一类失败从任何路径返回同一个
  `code` + `safe_message`（验收清单 #1）。未登记但"看起来像错误码"的值保留原码
  （排障），文案落回通用安全文案。
* **模型侧错误码 → 帧内 `status`**（`app/api/v1/chat.py::stream_error_frame`，与 HTTP
  路径同一套判定）：`MODEL_INSUFFICIENT_BALANCE`→402、`MODEL_AUTH_ERROR`→401、
  `MODEL_API_KEY_MISSING`→**400**、`MODEL_NOT_FOUND`→404、`MODEL_CONFIG_ERROR`→400、
  `MODEL_TOOL_CALL_UNSUPPORTED`→422、`MODEL_PROVIDER_UNAVAILABLE` / `MODEL_CONNECTION_ERROR` /
  `MODEL_UNAVAILABLE`→503；其余异常 → `500 CHAT_STREAM_INTERNAL_ERROR`。
  缺密钥走 400（`model.credentials_missing`，business 且不可重试）而不是 401：401 会被
  前端当成登录态失效而触发重新登录，而这里要做的是"去设置里填 API Key"。
  同一失败在非流式入口是 HTTP 400 + `data.error_code = MODEL_API_KEY_MISSING`
  （带 `data.byok` / `data.base_url`），两条路径的 code/文案必须逐字一致。
* 失败/取消的 `control` 帧同样带 `error_code`（同一个码）与 `safe_next_action`。
* **前端文案表**：只展示 `safe_message`；`trace_id` / `event_id` / `seq` / `error_code`
  只进日志。收到未知 `code` 时回落到 `safe_message`，不要自己拼文案。

### 10.2 终态封印（取消后不回跳）

* 后端闸门一（流内）：`StreamSeal`——同一条流出现终态帧后，**内容类**帧
  （`text_delta/process/step_*/tool_*/artifact_created/view_updated/approval_required/
  capability_*/operation_*`）不再外发。
* 后端闸门二（任务级，Redis `job_seal:{job_id}`，TTL 1h）：`POST /jobs/{id}/cancel`
  受理即封印；`record_frames` 不再收录迟到内容帧，因此**补拉接口**
  （`GET /agents/jobs/{id}/events?after_seq=`）也拿不到它们；任务定局（任何终态帧落盘）
  会自动封印；`resume` 会解除封印。
* 取消的标准形态：`control(state=cancelled)` + 兼容 `done`（旧前端以 `done` 结束流式）。
* 前端闸门：见 10.4 第 2 条。

### 10.3 视图与产物

* `view_updated.data` 超过 64KB / 嵌套 10 层 / 1000 个元素时：`data` 置空、
  `data_ref = "view:{view_id}"`（或调用方给的值）、`truncated=true`。**两种投影都执行**，
  旧投影不会把大 JSON 塞进事件流。
* `artifact_created` 只给引用与元数据。下载分两步（方案 §5.1）：
  1. `GET /api/v1/artifacts/{artifact_id}/download-url` → `{url, expires_at, expires_in}`
     （默认 300s，`ARTIFACT_DOWNLOAD_URL_TTL_SECONDS`，相对路径，需登录）；
  2. `GET {url}`（带 `token` 查询参数）实际下载。
* 令牌绑定"产物 + 用户"，因此**泄露的 URL 换个人也打不开**（403）；过期 → 401 且
  `data.error_code = RESULT_REF_EXPIRED`；篡改/缺参 → 401；越权/不存在 → 404。
  过期是常态路径：前端自动重发一次 `download-url` 再下载，仍失败才提示。

### 10.4 前端改造清单（联调验收）

1. **唯一归一化入口**：`chat.js` 只解析 SSE 行，**不得**再直接读 `evt.content` /
   `evt.job_id`；一律交给 `normalizeStreamEvent`（`streamConsumer.js`），组件只读
   ViewModel。参考实现与字段清单见 `app/contracts/stream_view_model.py`。
2. **终态丢弃（与方案 §6.2 的一处修正）**：终态后只丢**内容类**事件，不要丢"一切非终态
   事件"——否则 `task_failed` 的兼容伴随帧 `error` 会被自己的 `control(failed)` 吃掉，
   错误文案就丢了。元数据帧（`title` / `summary` / `audio_ready` / `usage`）同理照常生效。
3. **错误展示**：`payload.safe_message` +（可选）`suggested_action`；`code` 只进日志。
   12 个冻结码建议各配一句前端兜底文案（后端已给默认文案，前端不必自己拼）。
4. **View 上限**：`truncated=true` 或 `data` 为空但有 `data_ref` → 从
   `GET /agents/jobs/{id}` 的 `run_view.views[view_id]` 取值，不要重试事件。
5. **下载两步走**：点击卡片 → `download-url` → 下载；401/403 → 重新签发一次 → 仍失败提示
   "无权访问或产物已过期"。
6. **未知事件降级**：`payload.unsupported === true` 或未知 `type` → 记一条
   "当前客户端版本不支持该事件"+ `trace_id`，不渲染、不白屏（验收清单 #5）。
7. **乱序/重复**：按 `seq` 排序、按 `event_id`（缺省 `job_id+seq`）去重（验收清单 #3）。
8. **双协议切换**：先用 `docs/fixtures/stream-events/*.json` 打通 canonical，
   再切 `STREAM_EVENT_PROTOCOL=canonical`；切换前后 ViewModel 必须逐字段一致。
9. **`schema_version`**：canonical 帧新增了 `schema_version`（载荷结构版本，当前都是 1）。
   它是加法字段，前端可以忽略，但**不要**把它当成协议版本（协议版本是 `version`）。
10. **`approval_required.target`**：这是审批对象（方案 §2.3 明确要求），不是内部组件标识，
    可以展示；`source` 才是内部字段，已被禁止。

### 10.5 联调 Fixture（`docs/fixtures/stream-events/`）

9 个场景，每个文件含 `input_events`（内部事件）、`legacy_frames`、`canonical_frames`、
`expect`（期望的终态/计数/被吞帧数）。由 `tests/contracts/test_stream_contract_fixtures.py`
生成与校验，因此不会与实现漂移；前端可直接喂给 `StreamConsumer`。

| 文件 | 场景 |
|---|---|
| `chat_simple.json` | 普通聊天：`text_delta × N → done` |
| `tool_task.json` | 工具：`step_started → process → step_completed → text_delta → done` |
| `approval_task.json` | 审批：`approval_required → control(waiting_approval) → approval_resolved → …` |
| `artifact_task.json` | 产物：`artifact_created`（只给引用）+ 两步下载 |
| `view_task.json` | 视图：小数据直发 / 超限数据只给 `data_ref` |
| `failure_task.json` | 失败：`control(failed)` + `error`（统一错误码） |
| `cancel_task.json` | 取消：终态封印吞掉迟到内容帧 |
| `out_of_order_duplicate.json` | 乱序 + 重复：排序、去重 |
| `unknown_event.json` | 未知事件：`unsupported` 标记 / 大对象只留哈希引用 |

### 10.6 与前端仓库的对齐核查（`E:\javaidea\lumi`，只读比对）

前端已按同一份方案实现了消费侧（`src/services/streamConsumer.js`、
`streamErrors.js`、`artifacts.js`、`electron/stream-fixtures.cases.cjs`）。
逐项比对结论：

**已一致（无需改动）**

| 契约点 | 前端 | 后端 |
|---|---|---|
| 公开字段白名单 | `['event_id','version','seq','type','trace_id','conversation_id','job_id','occurred_at','payload','schema_version']` | 同（§2 + `schema_version`） |
| 终态封印判据 | 内容类集合 + `capability_`/`operation_` 前缀，且注释写明"与后端 `job_event_seal.py` 同一份" | 同 |
| 12 个冻结错误码 | `UNIFIED_ERROR_LABELS` 12 个键 | `FROZEN_ERROR_CODES` 同集合 |
| 视图上限 | 65 536 / 10 / 1 000，`overflow: data_ref`，**由后端执行** | 同 |
| 错误载荷字段 | 读 `safe_message` / `category` / `retryable` / `detail_ref` / `safe_next_action` | 全部下发 |
| `approval_required.target` | 允许展示 | 允许（§10.4 第 10 条） |
| 补拉接口 | `GET /agents/jobs/{id}/events?after_seq=&limit=`，读 `last_seq`/`truncated`/`events[]` | 同路径同字段 |
| 协议版本门禁 | `version > 1` → `UNSUPPORTED_VERSION` 丢弃 | 见下（已修正为 1） |

**本轮发现并修正的三处后端不一致**

1. **协议版本**：后端 canonical 帧原本是 `version: 2`，而前端 `STREAM_EVENT_VERSION = 1`
   且对 `version > 1` 的帧**整帧丢弃**——一旦切 `canonical`，前端会拿到空回答。
   已把 `EVENT_ENVELOPE_VERSION` 改为 1（canonical 协议从 1 起算）。
2. **产物签发字段名**：前端冻结 `download_url / expires_at / expires_in`
   （`artifacts.js` 明确"只认 `download_url`，不猜别名"），后端原来只给 `url`
   → 每次下载都会 `ARTIFACT_SIGN_FAILED`。已改为 `download_url`（并保留同值 `url` 别名）。
3. **补拉响应元数据**：原来写死 `protocol:"canonical"`、`version:2`；
   现已按实际帧形状如实回报（双协议期可能是 legacy 帧），版本取自协议常量。

以上三点都有后端回归测试钉住（`tests/contracts/test_stream_contract_fixtures.py` 的
"前端冻结契约对齐"四例 + `tests/api/test_artifact_download_url.py` 的响应形状断言）。

> 结论：**前端不需要为这三处改代码**；切 `canonical` 之前请确认后端版本为 1
> （`EVENT_ENVELOPE_VERSION`）并跑一遍 `docs/fixtures/stream-events/` 的 fixture。


