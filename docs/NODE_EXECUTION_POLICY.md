# 节点规格与执行策略

节点由业务层提供可执行函数和事实声明；编排/执行引擎解释这些声明，统一处理可靠性、资源和观测。

## 分层边界

| 内容 | 归属 |
| --- | --- |
| LLM、OCR、查库等节点业务逻辑 | 业务适配层 |
| `resource_class`、副作用类型、幂等键 | 节点规格声明 |
| 重试、超时、退避、并发、失败传播、恢复 | 执行引擎 |
| 状态、Event History、锁、遥测 | 运行时/基础设施 |

`NodeSpec.execution` 是内核包中的类型化契约，禁止放入 Python 回调、正则、导入路径或任意脚本。写一次节点必须声明自然键或显式幂等键；无法证明幂等性的写操作不会获得自动重试。

## 策略解析

引擎默认值位于 `config/agent_policies/execution_defaults.yaml`，解析顺序为：

```text
全局默认 < 任务级 task_policy < 节点 execution 声明
```

每个冻结的 `JobSpec` 都把解析后的节点策略、版本和 SHA-256 摘要写入 `routing.policy_snapshot`。任务恢复时使用已持久化快照，不重新解释变更后的 YAML。

路由意图词汇位于 `routing_lexicon.yaml` 的 `intent_patterns`，代码只实现固定匹配算法；策略加载失败时词汇能力为空，不能静默启用旧的硬编码词表。

## 统一结果

执行引擎返回 `JobExecutionResult`：节点结果、`completed/partial/failed/degraded` 状态、失败明细、产物引用、指标和策略快照。大产物应存外部对象存储，状态和 Event History 只保留引用。

## 验证

```powershell
.venv\Scripts\python.exe -m pytest packages/orchestration/tests/test_execution_spec.py tests/test_execution_policy.py -q
```

