# 编排状态与监控目录契约

本轮先建立边界，不一次性搬空 `orchestrator.py`。

## 目录

```text
app/agents/orchestration/state_machine/
  transitions.py  Job 合法状态转换，纯函数
  errors.py       错误分类与恢复提示
  policies.py     终态、重试、重规划、升级策略

app/monitoring/
  context.py      request/trace/job/node 关联字段
  events.py       MonitorEvent 结构化事件与脱敏
  exceptions.py   可管理的监控异常类型
  logger.py       Loguru 兼容的统一日志入口
  metrics.py      Prometheus 兼容代理
```

## 使用约定

- 状态写入前调用 `state_machine.transition(job, target)`；持久化、时间戳和审计仍由调用方负责。
- 终态任务不可原地恢复或改写；需要继续执行时创建新的 execution/fork。
- 日志通过 `monitor_logger.record()` 或其 `warning/error/exception` 快捷方法写入。
- `MonitorEvent.metadata` 不得放入 API key、token、密码、完整 prompt、文件正文或完整用户输入；这些键会被自动脱敏，但调用方仍应尽量不传。
- 关联字段使用 `MonitorContext`，日志中的 ID 会限制长度，避免异常日志扩大或泄露上下文。
- 监控故障必须 fail-open，不得阻塞任务主流程。

## 后续迁移顺序

1. 将 `JobFinalizer` 的终态事件接入 `MonitorEvent`。
2. 将 `orchestrator.py` 的 `_run_job`、重规划和升级异常收敛到 `classify_error()`。
3. 将 `cancel/pause/resume/approve` 控制面全部改为状态机入口。
4. 最后再把 API 异常处理和管理查询接到持久化监控日志表；本轮不新增数据库表，避免与现有 `control_log` API 混淆。
