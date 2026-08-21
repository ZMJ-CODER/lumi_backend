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

## 数据边界

`ManifestWorkflow` 的输入和 Continue-As-New 状态只包含 `job_id`、批次计数、超时和控制标志。完整 Job、清单、节点结果和进度仍持久化在 Redis；BYOK API key 只保存在短 TTL Redis bridge，绝不进入 Temporal history。

每批由 `run_manifest_batch_activity` 执行。Activity 内复用既有 `execute_dag`、资源锁、通道限流、副作用日志及 `LangGraphNodeRunner`。Activity 会 Heartbeat 并续租任务准入槽；Workflow 在指定批次数后 Continue-As-New，避免长清单的 history 无界增长。

若批次 Activity 的重试耗尽，Workflow 会调用轻量补偿 Activity，将 Redis 中仍在运行的 Job 收敛为 `failed` 并释放准入槽。这样 Workflow 层失败不会让前端永久卡在“执行中”；用户已取消或暂停的状态优先，不会被该补偿覆盖。

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

L3 替代子图、L2 Signal/Update 审批、写操作清单和完全退役 legacy 必须在以上验收完成后再迁移。
