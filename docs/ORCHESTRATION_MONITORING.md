# 编排监控与异常日志

> 更新：2026-08-23

监控位于 `app/monitoring/`，编排执行谱系位于
`app/agents/orchestration/execution_lineage.py`。两者都只记录排障所需的结构化元数据，
不会持久化完整 prompt、工具正文、附件内容或密钥。

## 1. 目录与职责

```text
app/monitoring/
  context.py     # request / trace / job / execution / node 关联 ID
  events.py      # MonitorEvent 与 metadata 脱敏
  logger.py      # monitor_logger，监控失败 fail-open
  metrics.py     # Prometheus 兼容指标代理
  exceptions.py  # 可管理的异常分类

app/agents/orchestration/
  execution_lineage.py  # 节点生命周期 span、result_ref
  effects.py            # effect journal 恢复扫描
  admission_lease.py    # 准入/等待状态 lease 维护
```

## 2. 必须关联的字段

所有结构化事件优先使用 `MonitorContext`：`request_id`、`trace_id`、`job_id`、
`execution_id`、`user_id`、`node_id`、`component`、`runtime`。字段会限长；调用方仍不得
向 metadata 传入 API key、token、密码、完整用户输入、prompt、文件正文或工具原始输出。

监控写入失败必须 fail-open，不能影响用户任务；但写资源、审批和 effect journal 的持久化失败
不是“监控失败”，它们必须 fail-closed。

## 3. 关键 span 与事件

| 事件 | 位置 | 内容 |
| --- | --- | --- |
| 节点生命周期 | `record_node_span` | 节点、公开工具名、参数哈希、状态、错误码、effect 状态、result_ref |
| 文档定位 | `tool_metadata.document_selection` | 被盘点/读取的已授权文档与选择理由，不含正文 |
| 工具候选选择 | `tool_metadata.selection_traces`（办公）或 monitor event（聊天） | `routing_mode`、候选 name/version/score/bootstrap、轮次、最终调用、未调用候选 |
| 低置信候选 | `TOOL_CANDIDATE_LOW_CONFIDENCE` | 明确工具意图的可能召回漏失；不记录原始请求 |
| 词法降级 | `lumi_skill_routing_modes_total{mode="lexical_fallback"}` | 语义索引未就绪/失败的占比；需检查 embedding 预热而非把它当随机模型行为 |
| bootstrap 即将到期 | `BOOTSTRAP_EXPIRING` | 到期前三天审阅候选命中/选择率并修复工具契约 |
| 副作用恢复 | `EFFECT_UNCERTAIN` | 有 intent 无 confirm，禁止自动重试 |
| 等待状态 | `waiting_resources` / `waiting_approval` | 挂起原因、超时和恢复时的准入重申请 |

候选选择 trace 用于区分“正确工具没被召回”和“工具已注入但模型没选”，不能用作思维链展示。

## 4. 运维查询

```powershell
# 任务状态及公开 span（须使用有权限的用户令牌）
GET /api/v1/agents/jobs/{job_id}
GET /api/v1/agents/jobs/{job_id}/spans

# Compose 中的结构化日志筛选
docker compose logs --since=1h api worker | Select-String `
  'EFFECT_UNCERTAIN|TOOL_CANDIDATE_LOW_CONFIDENCE|waiting_resources|waiting_approval'

# Prometheus 文本指标
curl http://localhost:8000/metrics
```

完整部署和恢复验收见 [ORCHESTRATION_DEPLOYMENT_GUIDE.md](ORCHESTRATION_DEPLOYMENT_GUIDE.md)。
