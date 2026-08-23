# 编排内核与策略部署指南

本文适用于本仓库的单 Compose 部署。编排内核位于
`packages/orchestration/src/lumi_orch`，业务适配位于 `app/agents/orchestration`，
策略资产位于 `config/agent_policies`。

## 部署原则

- 数据库 DDL 仅由一次性 `migrate` 服务执行；API 副本不在启动时建表或改表。
- 新策略默认运行在 `shadow`：只计算、记录新旧路由差异，不生成第二份计划、不调用模型或工具。
- 仅在影子日志和回归结果满足验收标准后，将策略切到 `enforce`。
- `lumi_orch` 不导入 `app.*`。Redis、LLM、审批、Worker、Skill、文档和监控都由应用适配层提供。

## 策略资产

| 文件 | 用途 | 不能做什么 |
| --- | --- | --- |
| `routing_rules.yaml` | 原子任务到四通道的路由规则 | 不可运行表达式、导入模块、调用工具或绕过安全检查 |
| `routing_lexicon.yaml` | 动作/对象词典 | 不可新增未注册动作/对象、权限或工具 |
| `tca_rules.yaml` | 复杂度评估数值权重与阈值 | 不可放入正则、提示词或路由动作 |
| `planning_rules.yaml` | 模板、脚本、半结构快捷路径 marker | 不可定义 DAG、节点、依赖、执行器或审批 |

所有 YAML 均在进程启动加载并经 Pydantic 校验。不是热加载配置；修改等同于代码变更，需回归、重新构建和重启。

## 首次或常规部署

在 `E:\pythonpycharm\lumi_backend` 执行：

```powershell
# 推荐：先保持影子模式
$env:AGENT_ROUTING_POLICY_MODE = "shadow"

# 重建并启动。migrate 成功结束后 API 才会启动。
docker compose up -d --build

# 查看状态；migrate 预期为 exited (0)，其他常驻服务预期为 running。
docker compose ps

# 检查迁移和策略加载。
docker compose logs --tail=120 migrate api

# 迁移版本必须包含 PostgreSQL 副作用 journal（0010_effect_journal）。
docker compose exec -T api alembic current
docker compose exec -T api alembic heads

# 健康检查应返回 database=ok、redis=ok。
Invoke-RestMethod http://localhost:8000/api/v1/health
```

成功部署的最低条件：

```text
migrate: exited (0)
api: running / healthy
health: database=ok, redis=ok
日志中不存在：ROUTING_POLICY_LOAD_FAILED、TCA_POLICY_LOAD_FAILED、
ROUTING_LEXICON_LOAD_FAILED、PLANNING_POLICY_LOAD_FAILED
alembic current/head: 0010_effect_journal
```

`0010_effect_journal` 建立外部副作用的 PostgreSQL 两段式 journal。若该迁移未完成，
写节点会以 `EFFECT_JOURNAL_UNAVAILABLE` 安全拒绝，而不会在没有可恢复 intent 的情况下
执行工具体。部署后可用下列只读日志筛查恢复扫描与 fail-closed 告警：

```powershell
docker compose logs --since=1h api worker | Select-String `
  "副作用日志恢复扫描|EFFECT_JOURNAL_UNAVAILABLE|EFFECT_UNCERTAIN|遗留副作用 intent"
```

Dockerfile 使用 `COPY config ./config`，因此上述四份策略文件会进入 API 和 Worker 镜像，无需额外挂载。

## Shadow 验收

保持 `AGENT_ROUTING_POLICY_MODE=shadow` 至少一个观察周期。查看路由审计事件：

```powershell
docker compose logs --since=24h api | Select-String `
  "ROUTING_POLICY_SHADOW_MATCH|ROUTING_POLICY_SHADOW_DIVERGENCE|ROUTING_POLICY.*FAILED"
```

验收要求：

- 没有策略加载或求值失败；
- 差异逐条人工归类；
- 预期差异只能是已批准的改进，不能涉及权限放宽、外部写入或审批遗漏；
- 多文档定位、单文件转换、文档检索、普通直答均有对应回归；
- 影子模式绝不执行第二份 DAG。

## 切换 enforce

只在 Shadow 验收通过后，在 `.env` 设置：

```env
AGENT_ROUTING_POLICY_MODE=enforce
```

然后仅重建会消费策略的服务：

```powershell
docker compose up -d --build api worker worker-best-effort worker-maintenance beat
docker compose ps
docker compose logs --tail=120 api
```

`enforce` 仅让通过策略校验的原子通道路由生效。审批、授权、资源锁、Plan Compiler、Worker/Skill 可用性检查依然在其后执行，策略不能绕过它们。

## 回滚

若发现路由异常、策略加载失败或行为退化，将 `.env` 改回：

```env
AGENT_ROUTING_POLICY_MODE=shadow
```

然后重启消费者：

```powershell
docker compose up -d --build api worker worker-best-effort worker-maintenance beat
```

若 YAML 本身损坏，运行时会记录对应 `*_POLICY_LOAD_FAILED` 并回退兼容路径；仍应尽快修复配置并重新部署，不应长期依赖回退。

## 本地回归（部署前）

尚未安装 workspace 包时可临时使用：

```powershell
$env:PYTHONPATH = "packages/orchestration/src"
.\.venv\Scripts\python.exe -m pytest `
  packages/orchestration/tests `
  tests/test_planning_policy.py `
  tests/test_routing_lexicon.py `
  tests/test_routing_policy.py `
  tests/test_planner_routing_intent.py `
  tests/test_tca.py `
  tests/test_plan_compiler.py `
  tests/test_logical_plan.py `
  tests/test_routing_upgrade.py `
  tests/test_state_machine.py `
  tests/test_temporal_manifest_runtime.py -q
```

副作用 journal 与多文档覆盖核验变更还应运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q `
  tests/test_effect_journal.py `
  tests/test_document_targeting.py `
  tests/test_execution_lineage.py `
  tests/test_approval_service.py

# Skill 候选召回、ReAct 工具选择与静态契约
.\.venv\Scripts\python.exe -m pytest -q `
  tests/test_tool_selection_contract.py `
  tests/test_langgraph_chat.py `
  tests/test_react_runner.py `
  tests/test_skills.py

# 当前 CI 静态检查
uvx ruff check app tests
```

安装/锁定 workspace 依赖必须在明确需要时执行：

```powershell
$env:UV_CACHE_DIR = "$env:TEMP\lumi-uv-cache"
uv lock
uv sync
```
