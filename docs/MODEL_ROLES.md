# 模型档位与职责角色（Model Profile + Model Role）

> 状态：**Phase 1 配置层 + Phase 2 低风险职责切换已落地**（后端）。
> 落点：`app/core/model_roles.py`（档位/角色/解析）、`app/core/model_plan.py`（任务级冻结）、
> `app/core/llm.py`（`role=` 入口）、`app/api/v1/admin.py`（后台切换接口）。

把"按场景选模型"升级为"**按任务职责选模型**"：业务代码只说"我需要什么角色"，
模型/端点/密钥由档位配置决定，换模型只改一处。

## 1. 两层概念

**Model Profile（档位）**

| 档位 | 用途 | 未显式配置时的来源 |
|---|---|---|
| `main` | 默认主模型：最终回答、复杂任务、工作区写入、代码生成 | `LLM_PROVIDER` + `DEEPSEEK_MODEL` / `QWEN_MODEL` |
| `cheap` | 分类、摘要、标题、改写、记忆处理、简单读取 | `DS_FLASH_MODEL` → `QWEN_TURBO_MODEL` |
| `reasoning` | 复杂规划、代码分析、动态 Agent、执行与修复 | `CHAT_THINK_MODEL`（空则复用 main） |
| `vision` | 图片、扫描件、PPT 页面视觉理解 | `VL_MODEL` |
| `embedding` | 向量化（**不参与聊天路由**） | `EMBEDDING_MODEL` |

每个档位除四元组（provider / base_url / api_key / model）外还声明能力：
`timeout`、`max_tokens`、`max_context`、`supports_tools`、`supports_json`、
`supports_vision`、`supports_reasoning`（可用 `LLM_{PROFILE}_*` 覆盖）。

**Model Role（职责）** —— 默认映射：

| 角色 | 档位 | | 角色 | 档位 |
|---|---|---|---|---|
| `title` | cheap | | `tool_read` | cheap |
| `summary` | cheap | | `tool_write` | main |
| `intent_assessor` | cheap | | `tool_execute` | reasoning |
| `query_rewriter` | cheap | | `direct_answer` | main |
| `memory_extract` / `memory_merge` | cheap | | `final_summary` | main |
| `privacy_candidate` | cheap（+后端规则） | | `code_writer` | main |
| `planner_simple` | cheap | | `code_reviewer` | reasoning |
| `planner_complex` | main | | `vision` | vision |

## 2. 解析优先级（高 → 低）

```
1. 请求级 BYOK（用户自备 key + 用户选择的模型端点）
2. 管理员动态角色配置   config:llm:role:{role}
3. 管理员动态档位配置   config:llm:profile:{profile}
4. 角色级 .env（LLM_ROLE_* + LLM_{PROFILE}_*）
5. 既有 scene 级 Redis 配置（兼容保留）
6. 旧全局 .env（LLM_PROVIDER / DEEPSEEK_MODEL …）
```

* **档位缺配置 → 回退 `main`**（"只配了 main 也能跑"）；
* **密钥只进内存/短期运行态**：`ResolvedModel.public_dict()`、Job 快照、SSE 元数据、
  管理视图都不含 api_key（只给"是否已配置"）；
* **角色只决定用哪个模型，不决定能不能执行**：是否有副作用、是否需要审批、
  是否越权仍由确定性规则判定（Router / Capability Broker / ApprovalPolicy）。

## 3. 已切换的调用点（Phase 2）

| 场景 | 角色 | 失败时的行为 |
|---|---|---|
| 会话标题 `_generate_title` | `title` | 返回空标题，不阻塞 |
| 会话摘要 `_generate_summary` | `summary` | 保留旧摘要，不阻塞 |
| 意图评估 `task_assessor` | `intent_assessor` | **cheap → main 重试一次 → 确定性启发式画像** |
| RAG 查询改写 | `query_rewriter` | 用原始查询 |
| 记忆抽取/会话段摘要 | `memory_extract` | 不写记忆（JSON 校验失败也丢弃） |

**未切换（按方案刻意保留）**：最终用户回答、代码生成/修改、工作区写入、
动态 ReAct 判断、代码审查 —— 仍走 `main` / `reasoning`。这些角色的档位已定义，
后续按 Phase 3/4 灰度。

## 4. 失败与回退规则

| 角色 | 失败处理 |
|---|---|
| `title` / `summary` / `memory_*` | 直接失败（调用方已有兜底），不升级模型 |
| `intent_assessor` / `planner_*` | 可重试错误或结构化输出不合法 → **换 main 重试一次** |
| `query_rewriter` | 用原始查询 |
| `tool_write` / `tool_execute` / `code_*` | 换 main 重试；仍失败即失败（不允许低成本模型继续） |
| `vision` | 换 main（不会静默用纯文本模型处理图片） |

附加约束：

* **工具能力**：`chat_with_tools(role=...)` 会校验档位是否声明支持工具调用；
  声明不支持时按角色回退策略升级到 `main`，**不会**静默降级成"没有工具的模型"；
* **流式不换模型**：`chat_stream` 只在**首 token 之前**允许回退，已经吐出内容后不再切换；
* 既有熔断器、超时、空输出检测、工具调用格式检查、JSON/Pydantic 校验全部保留。

## 5. 任务级冻结（ModelPlan）

任务创建时解析一次并冻结：

```
ModelPlan{ plan_id, version, created_at, byok, scene, roles:{role → {profile,provider,model,source}} }
```

* 公开部分写入 Job `routing["model_plan"]`（**无密钥**），前端可展示；
* 运行态副本存 Redis（`llm_plan:{plan_id}`，TTL 6h），只存角色/档位/超时/能力；
  服务端密钥**不复制**——调用时按档位现取，因此"轮换 key"立即生效，而"换模型"不影响在跑的任务；
* 重试/恢复读回同一份计划；**只有新任务**才使用新配置；
* 执行链想用冻结配置时调用 `model_plan_llm_config(plan, role)` 得到可直接传给
  `LLMClient` 的 `llm_config`。

## 6. 后台切换（无需重启）

### 6.1 前端契约（已确认，前端无需改动）

路径与请求/响应形状以 `src/services/adminSystem.js::LLM_CONFIG_ENDPOINTS` 为准，
后端已对齐（**不再使用** §6.3 的旧命名，旧命名仅作兼容别名保留）：

| 方法 + 路径 | 作用 | 请求体 |
|---|---|---|
| `GET /api/v1/admin/llm-config/models` | 当前生效的档位数组 + 角色映射（密钥脱敏） | — |
| `PUT /api/v1/admin/llm-config/models` | 局部保存：`{profiles?: {档位: {字段子集}}, roles?: {角色: 档位}}` | 两个键都可选、可单发 |
| `POST /api/v1/admin/llm-config/models/reset` | 重置：`{}` = 全部；`{profile: "cheap"}` = 单档位 | `ModelRolesResetRequest` |
| `POST /api/v1/admin/llm-config/models/test` | 连通性测试：`{profile}`，返回 `{ok, error, latency_ms}` | `ModelRolesTestRequest` |

响应形状（`normalizeProfileConfig` 依赖，逐字段钉死在
`tests/test_model_role_admin_api.py::test_read_view_shape_matches_frontend_normalizer`）：

```jsonc
// HTTP body（前端 authRequest 返回整个 body，取 body.data）
{"code": 0, "data": {
  "profiles": [{
    "profile": "cheap",
    "provider": "deepseek", "model": "deepseek-chat",
    "base_url": "https://api.deepseek.com/v1",
    "has_api_key": true, "api_key_masked": "sk-…abcd", "api_key_last4": "abcd",
    "connectivity": "unknown|ok|error", "last_error": "", "latency_ms": 0,
    "config_source": "admin|env|default",
    "capabilities": {                    // 前端命名（毫秒 / token）
      "timeout_ms": 60000, "max_output_tokens": 4096, "max_context_tokens": 32000,
      "supports_tools": true, "supports_json": true,
      "supports_vision": false, "supports_reasoning": false
    },
    "role_overrides": {"summary": "cheap"}   // 该档位被哪些角色使用
  }],
  "roles": {"title": "main", "summary": "cheap", "...": "…"}   // 角色 → 档位字符串
}}
```

`PUT` 的档位字段**接受前端命名**（`timeout_ms` / `max_output_tokens` /
`max_context_tokens` / `supports_*`）也接受内部命名（`timeout` / `max_tokens` /
`max_context`）；未提交的字段保持原值（真正的局部更新）。保存后立即生效，
返回 `data.profiles` / `data.roles` 为本次改动的名字列表。

### 6.2 鉴权（已确认）

写操作沿用本仓库既有范式，与 `/admin/rag-config` 完全一致：

1. 先 `POST /api/v1/admin/verify-password {admin_password}` 拿到 `data.verified_token`；
2. 之后每个写请求带 `X-Admin-Token: <verified_token>`，同时带超管 JWT。

即 `Depends(require_superadmin)` + `Depends(get_admin_verified_token)` +
`_require_admin_verified(x_admin_token, payload)`；缺失/过期令牌会被拒绝（文案含"二次验证"）。
`GET` 只需要超管 JWT（配置页首屏可能还没做二次验证）。

### 6.3 兼容别名（后端保留，前端不要新增使用）

| 旧接口 | 等价于 |
|---|---|
| `GET /api/v1/admin/model-roles` | §6.1 的 GET |
| `PUT /api/v1/admin/model-roles/profile/{profile}` | PUT（单档位形式，`reset=true` 清除、`test_only=true` 只测连接） |
| `PUT /api/v1/admin/model-roles/role/{role}` | PUT（单角色形式，空值 = 回落 `LLM_ROLE_*`） |
| `POST /api/v1/admin/model-roles/reset` | §6.1 的 reset |
| `GET/PUT /api/v1/admin/llm-config`（按 scene） | 更早的场景级配置，保留为兼容回退，不参与档位解析优先级 |

写 Redis 后进程内缓存立即失效，**5 秒内全局生效、无需重启**。

## 7. 遥测（成本对比）

`llm_usage` 表新增（迁移 `0014_llm_usage_model_roles`，全部可空，老库自动降级）：

| 列 | 含义 |
|---|---|
| `model_role` | 逻辑角色（title/summary/intent_assessor/…） |
| `model_profile` | 档位（main/cheap/reasoning/vision） |
| `config_source` | 配置来源（default/env/admin/byok/model_plan:*） |
| `fallback_used` | 是否发生角色/供应商回退 |
| `duration_ms` | 调用耗时 |
| `structured_ok` / `tool_calls` | 结构化输出是否通过、工具调用数 |
| `complexity` | 任务复杂度（可选写入） |

据此可以直接比较：每类角色的调用次数、token、失败率、fallback 比例、
首 token 延迟、Planner 编译失败率、工具成功率、单任务平均成本。

## 8. 前端需要做的（三类，不新增模型事件流）

1. **管理员模型配置页**（扩展现有 LLM 配置页）：四档位卡片显示 Provider / 模型名 /
   Base URL / API Key 脱敏 / 连通性 / 最近错误 / 被哪些角色使用；支持修改、测试连接、
   保存、重置、查看当前生效配置（接口见 §6）。角色列表显示"角色 → 档位 → 模型"，
   允许把角色指回 `main`。
2. **任务诊断信息**：Job/消息元数据里可展示 `model_role` / `model_name` /
   `config_source` / `fallback_used`（来自 `routing["model_plan"]` 与用量记录）；
   普通用户只显示"本次使用：快速模型 / 标准模型 / 深度模型"（`role_label()`），
   不展示 Base URL、密钥、内部配置与完整堆栈。
3. **不新增模型事件流**：复用既有 Job 快照 / `task_router` / `step_*` / `done` /
   用量接口，模型信息作为可选元数据附带即可。

## 9. 落地顺序与现状

| Phase | 内容 | 状态 |
|---|---|---|
| 1 | 档位/角色配置层 + 解析优先级 + 冻结机制（不改行为可全指 main） | ✅ |
| 2 | 低风险中间职责切 cheap（标题/摘要/查询改写/记忆/意图评估） | ✅ |
| 3 | 简单读取与简单 Planner 切 cheap（`tool_read` / `planner_simple`） | 角色已定义，待灰度接线 |
| 4 | 按复杂度切 Planner 与工具域（写入 main、执行 reasoning） | 角色已定义，待灰度接线 |
| 5 | 后台控制与成本看板（灰度某角色、观察失败与回退、一键恢复） | 接口已就绪，看板待前端 |

回归：`tests/test_model_roles.py`（档位解析 / 角色映射 / 动态覆盖 / 密钥不外发 /
计划冻结），旧 LLM 相关用例全部保持通过。
