# 异步任务分派契约

本项目不以“能异步运行”为唯一分类标准。每个任务先按可丢失性、幂等性、执行时长和是否需要跨进程恢复分类，再选择容器。禁止将任务为了方便随意塞进默认队列或请求内协程。

| 容器 | 任务 | 语义 | 失败处理 |
| --- | --- | --- | --- |
| Temporal | 办公 DAG、清单、审批、暂停/恢复 | 不丢失，工作流历史恢复 | Temporal 重试/心跳/补偿 |
| Celery `durable` | 文档解析、嵌入、索引重建、账户物理删除 | 至少一次 | 数据库幂等领取、Celery 重试、watchdog 重投 |
| Celery `best_effort` | 记忆抽取、会话摘要、画像重建、记忆触达 | 可延后，必须可补偿 | 有界重试；下次对话或定时重建补偿 |
| Celery `maintenance` | 清理、聚合、超时文档扫描 | 低优先级 | 幂等重试，不得阻塞前两类 |
| 进程内 asyncio | SSE、缓存失效、非关键遥测 | 允许丢失 | 无需重试或由下一请求补偿 |
| Redis Pub/Sub | UI 进度与通知 | 允许丢失 | UI 以 Job/DB 状态查询兜底 |

## Celery 可靠性约束

- Redis broker 使用 `task_acks_late=true`、`task_reject_on_worker_lost=true`、`worker_prefetch_multiplier=1`，确保 worker 死亡时消息可重新领取，且长文档不会被预取后饿死。
- `CELERY_REDIS_VISIBILITY_TIMEOUT_SECONDS` 必须大于 `CELERY_TASK_TIME_LIMIT_SECONDS`；默认分别为 45 分钟和 30 分钟。
- `documents` 保存 `celery_task_id`、排队/领取时间和尝试次数。处理前用行锁将状态置为 `processing`；重复投递只确认不重复运行。
- 每十分钟的 `recover_stale_documents` 将超时的 `pending/processing` 文档重新入队。它是 broker redelivery 的业务兜底，而非替代 Celery ACK。

## 监控与告警

`GET /metrics` 暴露：

- `lumi_celery_queue_ready_tasks{queue}`：Redis 就绪消息数。它不包含正在执行的任务。
- `lumi_document_pipeline_documents{status}`：`pending`、`processing`、`ready`、`error` 文档数。
- `lumi_document_pipeline_oldest_age_seconds{status}`：最老排队/执行文档的年龄。

告警优先看“最老等待时间”和状态量的组合，不能仅按 Redis `LLEN` 判断健康。`LLEN=0` 但 `processing` 的最老年龄持续增长，表示 worker 或外部依赖已经卡住。

## 升级到专用 MQ 的触发线

当前不引入 Kafka 或 RabbitMQ。出现任意一项才做专项评估：

1. 一个业务事件需要被多个独立服务消费，且它们必须独立演进。
2. 新消费者需要回放历史事件。
3. Redis 队列积压达到小时级，或队列内存已成为 Redis 的主要压力来源。

届时先确定事件契约和消费者幂等键；更换 broker 本身不会修复重复消费或副作用一致性问题。
