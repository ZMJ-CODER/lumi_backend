# 执行分支与前缀回放

## 目标和边界

执行分支用于从一个已结束的办公任务创建新的执行，而不是修改历史任务。它解决两类不同问题：

- **排障**：通过节点生命周期 span 还原当时的路由、工具、状态和耗时；不重新调用模型。
- **换方案重做**：从用户选择的节点开始，以新的参数或原子指令运行新的分支；原执行保留以便对照。

它不是隐藏思维链导出功能。span 不保存完整 prompt、模型正文、工具原始返回、API Key 或其他密钥。

当前 API：

```text
GET  /api/v1/agents/jobs/{job_id}/spans
POST /api/v1/agents/jobs/{job_id}/fork
```

两个接口都先验证任务归属。`fork` 的请求体是：

```json
{
  "node_id": "目标节点 ID",
  "params": {"可选参数覆盖": "值"},
  "instruction": "可选的新原子指令"
}
```

## 前缀重放语义

分支任务有新的 `job_id` 和 `execution_id`，同时持有：

- `parent_execution_id`：直接来源；
- `root_execution_id`：最初执行；
- `forked_from_node_id`：新的执行起点；
- `routing.fork`：父任务、起点及前缀节点 ID 的审计信息。

被选节点及所有下游节点会重置到 `pending`，并按既有 DAG、审批、资源锁、MCP Gateway 和副作用幂等日志重新执行。选点以外的已完成节点成为不可变前缀：新任务快照中 `result` 为 `null`，只有：

```json
{"result_ref": {"id": "opaque-id", "sha256": "content-hash"}}
```

结果正文留在按用户隔离的 Redis 结果仓中。只有实际依赖该前缀的下游节点在执行时才解析引用，解析会校验所有者和内容哈希。结果仓失效、哈希不匹配或前缀节点未完成时，分支会被拒绝，而不是用不完整事实继续运行。

这样即使有数十个前缀步骤，Job 快照和未来 Temporal workflow input 都不携带它们的结果正文，不会让任务历史随着回放线性膨胀。

## v1 副作用边界

v1 只支持前向重做，不能回到一个已提交副作用之前创建“替代历史”。如果所选节点的上游依赖里存在 `effect_status=committed`，创建分支会被拒绝。选中的节点本身如果已经提交副作用也会被拒绝。

这是有意的安全取舍：邮件已经发出、文件已经写入、系统记录已经修改时，重跑下游不等于撤销历史。补偿必须作为显式、可审批的工作流实现，不能在回放时自动猜测。

与选点没有依赖关系的已完成并行副作用可以保留在原分支中；新分支不复制也不重跑它们。

## 观测数据

节点开始和结束会追加紧凑 span，包含：

- execution/job/node ID；
- 事件、节点状态、执行 agent、公开工具名；
- 参数哈希、结果引用、错误码和副作用状态；
- 时间戳。

span 是面向前端任务详情、运维检索和分支对照的诊断索引，不是重放载荷。需要保留完整提示词或工具证据时，应进入经过脱敏、访问控制和保留期管理的专用审计系统，不能写进 Redis Job、Temporal history 或普通 API 响应。

## Temporal 迁移关系

当前默认仍为 `AGENT_ORCHESTRATION=legacy`，且 `manifest_temporal` 的滚动清单历史会压缩节点，暂不支持从单一节点 fork。前缀引用和执行谱系不依赖具体运行时：未来迁移到 Temporal 时，新的 Workflow 只接收 execution ID、目标节点和 `result_ref`；实际 LLM、RAG、MCP 和脚本调用继续在 Activity 内完成。

在 Temporal 版本中，分支应作为新的 Workflow 或 Child Workflow 运行，不能从旧 Workflow event history 注入正文或改写历史。
