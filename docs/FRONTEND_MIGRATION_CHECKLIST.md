# 前端迁移清单（后端结构重构 / 资源能力层收口）

本文把这一轮后端改造里**前端看得见的差异**收在一处，供前端排期。三类：
**必须改**（否则展示会错）、**可以不改**（加法或等价）、**可选跟进**（跨端排期用）。

数据来源：`docs/CAPABILITY_TWO_GENERATIONS.md`（两代对照与裁决）、
`docs/RESOURCE_CAPABILITY_LAYER.md`（标签契约）、`docs/ARCHITECTURE_BOUNDARIES.md`（结构重构）。

---

## 1. 必须改：管理端能力目录（`resource_catalog`）

接口：`GET /api/v1/admin/policies/...`（`admin_policies` 的能力总览，字段 `resource_catalog` /
`resource_entries` / `resource_unbound_tools`）。

| 字段 | 变化 | 前端要做什么 |
| --- | --- | --- |
| `providers[memory_provider].registered` | `true` → **`false`** | B-1 卡片按"**仅声明 / 待接入**"显示，与 `knowledge_provider` 一致；不要再暗示可用 |
| `providers[memory_provider].provider_id` | `lumi.server.memory` → **`""`** | 空值不要拼成 URL/文案，显示"待接入" |
| `providers[code_provider].execution_env` | `sandbox` → **`client`** | 该字段现在只表示"哪一侧执行"；沙箱属于运行方式（`runtime_kind`），别再按 `sandbox` 分支 |
| `providers[office_document_provider].provider_id` | `lumi.client.forwarder` → **`lumi.client.office_document`** | **不要**按 `provider_id` 分支；见第 2 节的稳定字段 |
| `entries[desktop_open_app\|desktop_open_url\|user_clarify].execution_env` | `server` → **`client`** | 本机动作在客户端执行（原来标服务端是错的） |
| `entries[task_memory].provider_candidates` | `["memory_provider"]` → **`[]`** | 空候选 = "没有实现，不可派发"，不要显示为可用 |

**判据**：`provider_candidates` / `accepted_provider_ids` 才是"能不能用"；
`resource_provider` / `provider_name` 只是"声明的路线"（可能尚未实现）。

## 2. 必须改：过程条目与 SSE 标签

契约：`ProcessLogEntry`（`packages/contracts/ts/lumi-contracts.d.ts` 已同步）。

| 字段 | 说明 |
| --- | --- |
| `provider_kind`（新增） | **稳定类别** = 资源类型（`workspace` / `office_document` / `memory` …），ASCII 闭集 |
| `provider_label`（新增） | **展示文案**（后端固定表给的中文，如"办公文档能力"） |
| `provider_id` | **改为"仅排障"**：迁移期会改名，不要再作为展示分支条件 |
| `provider_name` | 稳定逻辑名（`office_document_provider`），可继续用于分组 |

* 前端**推荐做法**：主文案用 `provider_label`，分组/筛选用 `provider_kind`，
  回退链 `provider_label → provider_kind → provider_name`；
* memory 相关的标签**不再带 `provider_id`**（仍带 `capability` / `resource_type` /
  `provider_name` / `provider_kind` / `provider_label`）；
* **历史行**：老步骤里已落盘的 `provider_id` 原样回放（不回写），并且会被补上
  `provider_kind` / `provider_label`——前端不必为老数据单独兜底；
* 老载荷（没有新字段）仍能解析；`exclude_none=True` 保证老条目的 JSON 一个键都不多。

## 3. 必须改：缺模型 API Key（`MODEL_API_KEY_MISSING`）

接口：`POST /api/v1/chat/stream`（SSE）、`POST /api/v1/conversations/{id}/messages`、
`POST /api/v1/agents/jobs`（规划阶段缺密钥 → 任务立刻 `failed`）。

| 出口 | 现在的形态 | 前端要做什么 |
| --- | --- | --- |
| 非流式 `messages` | HTTP **400**，`data.error_code = "MODEL_API_KEY_MISSING"`，`data.byok`（是否自带密钥）、`data.base_url` | 按 `error_code` 分支弹"去设置里填模型 API Key"，**不要**当作登录过期 |
| SSE `/chat/stream` | `{"type":"error","status":400,"code":"MODEL_API_KEY_MISSING","message":"当前使用「自带密钥」，请在设置里填写模型 API Key 后重试…"}` | 同上一行；此前这里是 `status=500 / code=CHAT_STREAM_INTERNAL_ERROR`（前端只能显示"服务器内部错误"） |
| 办公任务 | `job.status=failed`，`result.error_code = "MODEL_API_KEY_MISSING"`，`retryable=false` | 显示"填写 API Key"，不要自动重试、不要重放提交 |

* **BYOK 用户必须逐请求带 `x-llm-api-key`**：密钥不落库，服务端不缓存；漏带就是这个码；
* 401 才是"密钥无效/已失效"（`MODEL_AUTH_ERROR`），**不要把 400 当登录过期**去刷
  `/auth/refresh`——缺密钥刷多少次都不会好；
* 本地/内网端点（`localhost` / `127.0.0.1` / 内网 IP / `*.local`）**允许空密钥**，
  自建 Ollama / vLLM 不受这条守卫影响；
* `model.credentials_missing` 是统一错误模型里的域内码（`business`、不可重试），
  事件投影只给 `safe_message`，`code` 只进日志。

## 4. 必须改：管理面不再要"二次管理员密码"

**裁决（2026-09）**：管理员已经用 superadmin JWT 登录过，查看连接状态/改配置不该再输一遍
密码。后端已把 `/api/v1/admin/**` 的二次密码验证**整体移除**，授权只剩
`require_superadmin`（超管登录态）。

| 后端变化 | 前端要做什么 |
| --- | --- |
| 所有管理接口（含 `PUT /admin/rag-config`、`POST /admin/llm-config/models/test`、`POST /admin/policies/*`、`GET /admin/strategy-policies`）**不再校验** `X-Admin-Token` | 删掉密码输入框、`verifyAdminPassword()` 调用与 `X-Admin-Token` 头；直接调用接口 |
| `POST /admin/verify-password` **保留但已废弃**（响应多 `deprecated: true`） | 不再调用；旧版本客户端调用也不会 404 |
| 请求里带旧 `X-Admin-Token` 会被忽略 | 不用做兼容分支，带了也无副作用 |
| 非超管仍 403（`需要超级管理员权限`），未登录仍 401（`data.error_code=LOGIN_REQUIRED`） | 权限分支照旧，只删"二次密码"这一层 |

已经按这份清单改好的前端文件（本地 `E:\javaidea\lumi`，随 Vite 热更新生效）：

* `src/components/LlmRolesSettings.jsx`：删密码输入框 + `withAdminToken()`；保存/重置/测试
  直接调 `updateLlmRoleConfig(payload)` / `resetLlmRoleConfig(profile)` / `testLlmProfile(profile)`；
* `src/components/RuntimePolicyPanel.jsx`：删 `adminPassword` / `token` / `ensureToken()`；
  `upsertRuntimePolicy(payload)`、`deleteRuntimePolicy(field)`、`refreshRuntimePolicies()`、
  `grantWriteLease(payload)` 都不再传 token；
* `src/components/SystemAdmin.jsx`：RAG 配置保存不再要求密码（`updateRagConfig(cfg)`）；
* `src/services/adminSystem.js`：`updateLlmRoleConfig` / `resetLlmRoleConfig` / `testLlmProfile`
  / `updateRagConfig` 去掉 token 形参；`verifyAdminPassword` 标记 `@deprecated`；
* `src/services/adminPolicies.js`：去掉 `adminHeaders()` 与 token 形参。

**联调自检**（对着运行中的后端，只需登录态）：`POST /admin/llm-config/models/test {"profile":"main"}`
→ `{"ok":true,...}`；`POST /admin/policies/refresh` → 200；`PUT /admin/rag-config` → 200。

## 5. 可以不改：加法与等价

* `resource_catalog` 新增键：`native_action_tools`（本机动作清单）、
  `deferred_families`（刻意不接入的族）、`resource_labels`（资源类型 → 文案）、
  `providers[].legacy_provider_ids` / `accepted_provider_ids` / `label`；
* `POST /capabilities/invoke` 请求体**未变**（没有新增字段）；
* SSE 事件类型、错误码、帧结构**未变**；能力事件本来就带
  `execution_plane` / `runtime_kind` / `executor_type`；
* Broker 的 Provider 收窄两个新参数是**后端内部**的，默认不传 = 旧行为；
* routing 的 `off / shadow / read_only / active` 语义未变；
* 预检状态词表未变；只有"下一步"更准（设备没连 → `CONNECT_PROVIDER`，
  工作区绑错 → `BIND_WORKSPACE`，位置不允许 → `CHANGE_EXECUTION_PLACEMENT`）；
* 审批档位与"要不要确认"的判定未变（只是"注册表不接管的工具必须显式登记"这类内部约束被测试钉住）。

## 6. 可选跟进（桌面端，不阻塞）

`office_document_provider` 已从通用转发 id 独立出来，**兼容期同时接受新旧两个 id**
（收窄集合 = `{lumi.client.office_document, lumi.client.forwarder}`）：

* 桌面端**现在什么都不用改**；
* 方便时把办公文档能力的注册 id 切到 `lumi.client.office_document`（或双注册），
  后端不必再改；管理端能看到这条路还在接旧注册（`legacy_provider_ids`）。

## 7. 已经不需要前端做的事（本轮删掉的隐患）

* 不需要再维护"工具名 → 能力"的映射表：事件与过程条目直接给
  `capability` / `resource_type` / Provider 标签；
* 不需要为"能力声明了但没实现"做特殊兜底：这类状态现在在目录里显式标成
  `registered=false` + 空 `provider_id`，且**绝不会**出现在候选与事件标签里。
