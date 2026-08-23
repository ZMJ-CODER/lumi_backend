# MCP 与 Skill 治理

> 版本：v1.1（2026-08-23）

本文件描述 Skill 生命周期、候选工具路由和外部 MCP 工具准入。它们建立在现有
执行器的场景、角色、确认、沙箱、审计边界之上，不能替代这些边界。

## 1. 候选工具路由

工具选择分两阶段；候选召回与模型选择必须分开观测和评测：

```text
场景/角色/写开关/运行时/用户 MCP 绑定硬过滤
  -> 合法 Skill 池
  -> 词法规则 + 可选语义相似度 + 可靠性/成本排序
  -> 冲突消解和类别去重
  -> Planner 或 ReAct 的 Top-K Function Calling 工具
  -> 模型在已注入候选中调用或直接回答
```

语义相似度只用于合法池内排序，永远不能授权工具。以下规则仍保留优先级：

- 明确的文件转换、导出和创建请求优先粗粒度产物工具；
- `conflicts_with` 与 `preferred_over` 处理互斥能力；
- 普通聊天仍只允许问答白名单；
- 聊天只使用一次请求级候选池（最多 5 个）；办公 ReAct 每轮最多暴露 8 个工具，会结合最新工具观察刷新候选并剔除已失败工具。

每个候选选择都会产生安全 trace：场景、轮次、`routing_mode`、`name`、`version`、分数、是否 bootstrap、
最终调用工具与同轮未调用候选。trace 不包含用户原文、提示词、工具参数、思维链或工具正文。
办公任务将 trace 放入节点 `tool_metadata.selection_traces`；聊天任务仅记录结构化监控事件。

评测至少覆盖两层：`candidate_recall@K` 验证期望工具是否进入候选池，
`selection_accuracy_given_candidates` 验证给定候选后模型是否正确调用/不调用。明确工具意图却无候选
或仅低分候选时记录 `TOOL_CANDIDATE_LOW_CONFIDENCE`，并禁止回答伪称已从外部来源核验；该告警
不会把关键词变成强制联网规则。

`SKILL_SEMANTIC_ROUTING_ENABLED=true` 默认开启。应用启动和插件热更新后会同步预热小规模
Skill 描述索引；普通请求绝不承担 bge 加载或索引构建成本。若模型/索引不可用，候选 trace 的
`routing_mode=lexical_fallback` 会明确记录降级，Prometheus 的
`lumi_skill_routing_modes_total{scene,mode}` 可统计其占比；不能把它当作与语义模式等价的静默行为。

## 2. Skill 生命周期

每个 `Skill` 都有：

```text
name + version + status + schema_fingerprint
```

- `version` 必须符合 semver，例如 `2.1.0`；
- `status=stable` 才会进入自动候选池；
- `experimental` 只能由显式灰度路径调用；
- `deprecated` 不进入新计划，旧 Job 可在兼容期内由恢复策略处理；
- `disabled` 对任何场景不可见；
- `schema_fingerprint` 是名称、版本、参数 Schema 和执行环境的哈希，用于计划缓存、
  长任务恢复及审批排障时识别不可兼容变更。

插件热更新会同步重建语义索引，`POST /api/v1/admin/skills/reload` 返回
`semantic_routing_ready`；重建失败时服务继续使用有 trace 的词法降级。管理接口
`GET /api/v1/admin/skills` 会返回上述生命周期字段。

Skill 修改应在 CI 运行四类契约用例：正常输入、非法/边界参数、权限或确认拒绝、
运行时不可用。工具路由还应维护“自然语言请求 -> 期望工具在 Top-K”的评测集。

选择契约还包括 `use_when`、`do_not_use_when`、`selection_examples`、`handoff_to`。
插件注册时静态 lint 会拒绝不存在的相邻工具引用、缺少限定意图/无效日期的 bootstrap，以及
高重叠 `intent_tags` 却未声明关系的工具。`bootstrap_intents + bootstrap_until` 仅让新 Skill 在
限定意图、限定日期内优先进入候选池；不会全量注入聊天。选择提示从注册契约编译，避免手写提示词漂移。

## 3. 外部 MCP 工具准入

`MCP_SERVERS` 仅是部署侧连接白名单，不代表模型有权调用其中工具。外部工具需要：

```text
部署配置 allow_user_binding=true
  -> 当前用户发现该 Server 的具名工具
  -> 用户显式创建绑定
  -> 保存安全映射和 Schema 快照
  -> 该用户在 office 场景才进入合法候选池
```

绑定表为 `user_mcp_tool_bindings`。它引用 `server_name` 与 `raw_tool_name`，不接受
用户提交 URL，因此不会将 MCP 连接变成 SSRF 入口。外部工具名称固定为：

```text
mcp__{server_name}__{raw_tool_name}
```

外部 MCP 的远端 annotation 只能用于收紧策略的参考，不能降低平台安全级别。第一版
所有外部绑定都使用服务端 `tool + normalized_args` 指纹确认；写操作必定需要确认。
受控 Electron 内置 Skill 继续沿用现有客户端确认通道。

绑定管理接口：

```text
GET    /api/v1/mcp/servers/{server_name}/tools
GET    /api/v1/mcp/bindings
POST   /api/v1/mcp/bindings
POST   /api/v1/mcp/bindings/{binding_id}     # enabled true/false
DELETE /api/v1/mcp/bindings/{binding_id}
```

MCP Server 进入失败冷却或熔断时，不从合法池悄悄摘除；候选 trace 以
`availability_hint=circuit_breaker` 标明状态，调用层再返回结构化暂不可用错误。这样能区分
“工具可见但暂不可用”和“模型未选择”，也让模型如实向用户说明限制。撤销绑定只阻止后续节点调用，
已发生的外部副作用不会自动补偿。

每个绑定还由部署配置给出独立的每日调用与并发上限，默认分别为 100 和 2；可在
`MCP_SERVERS` 某一 Server 项用 `mcp_daily_call_limit`、`mcp_concurrency_limit` 覆盖。
Redis 配额不可用时外部 MCP 调用会失败关闭，避免失去限额后继续放大风险。

部署可按全局 `MCP_EXTERNAL_REQUIRE_ADMIN_APPROVAL=true`，或按单个 Server 的
`mcp_require_admin_approval=true` 开启二次准入。此时用户创建的绑定状态为
`pending_approval`，只有超级管理员通过 `GET /api/v1/admin/mcp/bindings/pending` 与
`POST /api/v1/admin/mcp/bindings/{id}/review` 批准后才可被模型看见。用户不能通过
“启用”接口绕开审核。撤销会阻止新调用，并尽力取消同一 API 进程中正在执行的调用；
跨进程或已在外部服务端开始的副作用仍以确认、幂等和外部服务语义为准。

## 4. 进度与观测

`SkillProgress` 统一表达 `started`、`awaiting_confirmation`、`executing`、`completed`、
`failed`、`cancelled` 六类状态，并携带 job/node/tool 标识。MCP 进度回调会映射为
该结构，再附带 MCP 的 `total` 字段以保持兼容。

`lumi_skill_calls_total` 保留调用结果指标。后续接入真实的分场景成功率与成本聚合后，
可将其作为相近候选之间的排序信号；不得将少量样本的成功率作为绝对禁用条件。

`skill_telemetry_daily` 现在按日期、Skill、版本、场景和规范化错误类型累积调用数、
成功数与时长。默认只在最近 30 天达到至少 10 次样本后，才把成功率注入候选排序的
轻量 tie-break，绝不自动禁用某个工具。所有已注册 Skill 同时受基础契约测试保护：
名称、生命周期、Schema 指纹与 Function Calling Schema 变更会在 CI 中失败。

遥测缓存仅在 API 启动时预热；候选选择路径只读内存缓存，不会因为遥测查询增加工具
调用的首轮延迟。Electron 的 Redis 兼容轮询采用 500ms 活跃、5 秒空闲、10 分钟后
30 秒低频的退避策略；MCP 直连不经过该轮询。

工具结果的 `confidence_hint` 只接受 `{level, basis[]}` 结构，其中 `basis` 必须来自 citation 数、
授权文档读取、供应商结果条数等可观察事实。没有可计算依据时省略置信提示，禁止实现侧写入任意
`high`/`medium`/`low` 字符串误导后续模型步骤。

bootstrap 到期前三天发出 `BOOTSTRAP_EXPIRING` 告警，并同时审阅该 Skill 的候选命中和模型
选择率；若它只能依靠 bootstrap 入池，应修复 `intent_tags`、`use_when` 或边界声明，不能直接续期。

## 5. 数据库迁移

执行部署迁移：

```powershell
docker compose run --rm migrate
```

应用进程不执行 `create_all` 或手写 DDL；开发和生产均在启动前运行 Alembic，确保
表、列、唯一约束和索引都由同一迁移链管理。
