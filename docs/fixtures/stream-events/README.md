# 事件契约 Fixture（前端联调用）

> **性质**：这是**提交进仓库的契约产物**（不是构建缓存）。后端运行时**不读**它，
> 只由测试生成/校验；前端仓库的 `electron/stream-fixtures.cases.cjs` 是它的镜像。
> 生成过程**逐字节稳定**（`event_id` / `occurred_at` 落盘前被钉成确定值），
> 跑测试不会改脏工作区。

由后端测试生成并校验，**不要手改**：

```powershell
$env:TEMP="E:\pythonpycharm\lumi_backend\.ptmp"; $env:MCP_SERVERS='[]'
.\.venv\Scripts\python.exe -m pytest tests/test_stream_contract_fixtures.py -q -p no:cacheprovider
```

生成器：`tests/test_stream_contract_fixtures.py`（场景与期望值都在里面）。
契约文档：`docs/STREAM_EVENT_PROTOCOL.md` §10。

## 文件结构

```jsonc
{
  "scenario": "cancel_task",
  "description": "取消：终态封印吞掉迟到内容帧",
  "input_events": [ /* 后端内部事件（联调排障用，前端不需要） */ ],
  "legacy_frames": [ /* 旧投影 SSE 帧（当前线上形状） */ ],
  "canonical_frames": [ /* 标准投影 SSE 帧（切换后用这个） */ ],
  "expect": {
    "canonical_types": ["step_started", "text_delta", "control", "done"],
    "terminal_state": "cancelled",
    "dropped_after_terminal": 2,
    "answer_text": "正在处理…",
    "absent_text": ["迟到正文不应出现"]
  }
}
```

## 前端怎么用

1. 把 `canonical_frames`（或 `legacy_frames`）逐条喂给 `StreamConsumer` 的归一化入口
   （等价于 `app/contracts/stream_view_model.py::consume_frames`）；
2. 断言归一化结果满足 `expect`：终态、正文、步骤状态、条目数、被丢弃帧数；
3. **同一场景两种投影的 ViewModel 必须一致**——这是"切换协议不改前端行为"的判据；
4. 场景覆盖：普通聊天 / 工具 / 审批 / 产物 / 视图 / 失败 / 取消（含迟到帧）/
   乱序重复 / 未知事件。

## 帧字段速查（canonical）

`event_id`（去重）、`version`（协议版本，当前 **1**；必须 ≤ 前端 `STREAM_EVENT_VERSION`）、`schema_version`（载荷结构版本，当前 1）、
`seq`（单调递增，断线补帧用）、`type`（11 种之一或既有透传类型）、`trace_id`、
`conversation_id`、`job_id`、`occurred_at`、`payload`（唯一负载）。

终态：`control`（`payload.state ∈ completed|failed|cancelled|interrupted|blocked`）
或 `done`；`task_failed → control(failed) + error`，`cancelled → control(cancelled) + done`。

