# Lumi Backend 前后端联调接口文档

> 适用于 Lumi - 桌面 AI 陪伴助手。本文档描述前端与服务端联调所需的全部 HTTP 接口，重点覆盖核心业务链路：**用户在前端发送消息 → 后端将消息传给大模型 → 后端将大模型回复转发给前端**。

---

## 1. 通用约定

### 1.1 Base URL

| 环境 | Base URL |
| ---- | -------- |
| 本地开发 | `http://localhost:8000/api/v1` |
| 生产环境 | 以部署为准 |

所有路由均挂在 `/api/v1` 前缀下（见 `app/main.py` / `app/api/router.py`）。

### 1.2 数据格式

- 请求/响应均为 `application/json`
- 统一响应包装结构：

```json
{
  "code": 0,
  "message": "success",
  "data": {}
}
```

其中 `code`：`0` 表示成功，非 `0` 表示业务错误；`message` 为提示信息；`data` 为业务数据。

### 1.3 认证方式（Bearer Token）

除「验证码 / 注册 / 登录 / 刷新令牌」外，其余接口均需携带 access_token：

```
Authorization: Bearer <access_token>
```

未携带或 token 过期/无效时返回 `401`；权限不足返回 `403`。

### 1.4 Token 体系

| 令牌 | 有效期 | 获取方式 | 用途 |
| ---- | ------ | -------- | ---- |
| `access_token` | 1 小时（`ACCESS_TOKEN_EXPIRE_SECONDS`） | 注册/登录/刷新 | 每次请求的 `Authorization` 头 |
| `refresh_token` | 30 天 | 注册/登录/刷新 | access_token 过期后，调用刷新接口换新对 |

> 轮换策略：每次刷新会废弃旧 refresh_token 并签发新对，旧 token 立即失效。

---

## 2. 认证模块（已实现 ✅）

> 源码：`app/api/v1/auth.py`

### 2.1 获取图形验证码

**`GET /auth/captcha`**

响应（`data` 为 base64 图片 + 验证码 ID）：

```json
{
  "code": 0,
  "data": {
    "captcha_id": "uuid",
    "image_base64": "data:image/png;base64,..."
  }
}
```

### 2.2 注册（注册即登录）

**`POST /auth/register`**

请求体（`captcha_result` 填验证码图上的算式**计算结果**，如图片显示 `13 + 7` 则填 `20`）：

```json
{
  "account": "user@example.com",
  "password": "abc12345",
  "captcha_id": "uuid",
  "captcha_result": "20"
}
```

响应：

```json
{
  "code": 0,
  "message": "注册成功",
  "data": {
    "user_id": "uuid",
    "username": "user@example.com",
    "access_token": "jwt...",
    "refresh_token": "opaque...",
    "expires_in": 3600,
    "user": {
      "user_id": "uuid",
      "username": "user@example.com",
      "avatar_url": "",
      "role": "user"
    }
  }
}
```

### 2.3 登录

**`POST /auth/login`**

请求体（同上，`captcha_result` 填算式计算结果）：

```json
{
  "account": "user@example.com",
  "password": "abc12345",
  "captcha_id": "uuid",
  "captcha_result": "20"
}
```

响应（同注册，无 `message` 业务包装时返回）：

```json
{
  "code": 0,
  "data": {
    "access_token": "jwt...",
    "refresh_token": "opaque...",
    "expires_in": 3600,
    "user": { "user_id": "uuid", "username": "...", "avatar_url": "", "role": "user" }
  }
}
```

### 2.4 刷新令牌（换新对）

**`POST /auth/refresh`**

请求体：

```json
{ "refresh_token": "opaque..." }
```

响应结构与登录一致，返回**全新的** access_token + refresh_token。

### 2.5 登出（废弃 refresh_token）

**`POST /auth/logout`**

请求体：

```json
{ "refresh_token": "opaque..." }
```

响应：

```json
{ "code": 0, "message": "已登出" }
```

---

## 3. 核心对话链路（重点 ⭐）

> 源码：`app/api/v1/conversations.py` + `app/services/orchestrator.py` + `app/agents/chat_agent.py`

### 3.1 业务流程图：用户消息 → 大模型回复

```
┌────────┐   ① POST /conversations/{conv_id}/messages    ┌──────────┐
│  前端   │ ────────────────────────────────────────────▶ │  FastAPI  │
│ (React) │                                               │ send_message │
└────────┘                                               └──────────┘
                                                               │
                                                           ② ③ ④ ⑤
                                                               ▼
                                                        ┌────────────────┐
                                                        │ Orchestrator   │
                                                        │ handle_message │
                                                        └────────────────┘
                                                          │   │    │    │
                                           ② 加载场景配置   │   │    │    │
                                           ③ Redis 上下文  │   │    │    │
                                           ④ 长期记忆注入   │   │    │    │
                                           ⑤ RAG 知识检索   │   │    │    │
                                                               ▼
                                                        ┌────────────────┐
                                                        │   LLM Client   │
                                                        │  (DeepSeek)    │
                                                        └────────────────┘
                                                               │
                                             ⑥ 大模型返回回复文本 │
                                                               ▼
                                                        ┌────────────────┐
                                                        │ Orchestrator   │
                                                        │ ⑦ 保存上下文   │
                                                        └────────────────┘
                                                               │
                             ⑧ 响应 {message_id, content,      │
                                citations, scene, local_mode}  │
┌────────┐   ⑨ 前端渲染 AI 回复   ◀────────────────────────────┘
│  前端   │ ◀─────────────────────────────────────────────
└────────┘
```

### 3.2 创建会话

**`POST /conversations`**（需认证）

请求体：

```json
{ "scene": "chat" }
```

`scene` 可选值：`chat`（闲聊）/ `office`（办公）/ `game`（游戏）

> ⚠️ 当前为占位实现：返回固定的 `conversation_id: "placeholder-conv-id"`，尚未写入数据库。

### 3.3 发送消息并获取大模型回复 ⭐（核心接口）

**`POST /conversations/{conversation_id}/messages`**（需认证）

请求体（`SendMessageRequest`，见 `app/models/conversation.py`）：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| ---- | ---- | ---- | ------ | ---- |
| `content` | string | ✅ | - | 用户发送给大模型的文本消息 |
| `scene` | string | ❌ | `office` | 场景模式：`chat` / `office` / `game`，决定 System Prompt、知识库检索范围、操控权限 |
| `local_mode` | bool | ❌ | `false` | 是否本地模式（PC 端已本地处理，仅同步摘要，不调用大模型） |
| `attachments` | list | ❌ | `[]` | 附件列表（预留） |

示例请求：

```json
{
  "content": "帮我写一份周报",
  "scene": "office",
  "local_mode": false
}
```

响应（`data` 即大模型回复，直接透传给前端展示）：

```json
{
  "code": 0,
  "data": {
    "message_id": "uuid",
    "content": "好的，这是你的周报：\n1. ...",
    "citations": [],
    "scene": "office",
    "local_mode": false
  }
}
```

| 返回字段 | 类型 | 说明 |
| -------- | ---- | ---- |
| `message_id` | string | 本条回复消息 ID |
| `content` | string | **大模型生成的回复文本（前端直接展示）** |
| `citations` | array | 引用来源列表 `[{type, title, content, source}]`（知识库 RAG 命中时返回） |
| `scene` | string | 本次回复使用的场景 |
| `local_mode` | bool | 是否本地模式处理 |

特殊分支：

- `local_mode=true` 时：后端不调用大模型，`content` 返回空字符串，`local_mode=true`。
- 内部处理流程（`orchestrator.handle_message()`）：
  1. 加载场景配置（System Prompt + 知识库标签）
  2. 从 Redis 加载会话上下文（最近 N 轮）+ 用户长期记忆
  3. RAG 检索知识库（按场景空间标签过滤）
  4. 拼接 messages → 调用大模型
  5. 保存 user/assistant 消息到 Redis 上下文
  6. 异步触发记忆提取（预留）

### 3.4 获取会话消息历史

**`GET /conversations/{conversation_id}/messages`**（需认证）

查询参数：

| 参数 | 类型 | 默认值 | 说明 |
| ---- | ---- | ------ | ---- |
| `limit` | int | 50 | 返回条数，1~200 |
| `before_message_id` | string | 无 | 游标分页：取该消息之前的历史 |

响应：

```json
{
  "code": 0,
  "data": {
    "items": [],
    "has_more": false
  }
}
```

> ⚠️ 当前为占位实现：`items` 恒为空数组，尚未接通 PostgreSQL。

### 3.5 删除会话

**`DELETE /conversations/{conversation_id}`**（需认证）

响应：

```json
{ "code": 0, "message": "已删除" }
```

会同时清除 Redis 中的会话上下文。

### 3.6 更新会话标题

**`PATCH /conversations/{conversation_id}`**（需认证）

请求体：

```json
{ "title": "新标题" }
```

### 3.7 获取会话列表

**`GET /conversations`**（需认证）

查询参数：`scene`（默认 `chat`）、`limit`（默认 20，≤100）、`offset`（默认 0）

响应：

```json
{
  "code": 0,
  "data": { "items": [], "total": 0, "limit": 20, "offset": 0 }
}
```

> ⚠️ 当前为占位实现：`items` 恒为空数组。

### 3.8 获取所有可用场景

**`GET /conversations/scenes`**

响应：

```json
{
  "code": 0,
  "data": {
    "scenes": [
      { "id": "chat", "name": "闲聊", "local_acceleration": false },
      { "id": "office", "name": "办公", "local_acceleration": false },
      { "id": "game", "name": "游戏", "local_acceleration": true }
    ]
  }
}
```

---

## 4. 大模型接入现状说明（实现状态）

### 4.1 已实现的真实大模型调用（可复用）

`app/agents/chat_agent.py` 中的 `ChatAgent.execute()` 已打通真实 LLM 调用链：

```
ChatAgent.execute() → LLMClient(provider="deepseek").chat() → POST https://api.deepseek.com/v1/chat/completions → 返回回复文本
```

- 模型：`deepseek-v4-flash`
- 带短期记忆（session 维度，内存中保留最近 10 轮）
- 支持 System Prompt 注入（按场景）
- 需要在 `.env` 中配置 `DEEPSEEK_API_KEY`

### 4.2 对话接口实现状态（已接通 LLM 链路）

`POST /conversations/{conversation_id}/messages` 内部调用的 `orchestrator.handle_message()`：

- `_call_llm()`：✅ **已接入真实 LLM**。通过 `LLMClient`（按 `.env` 中 `LLM_PROVIDER=deepseek` 配置）调用大模型生成回复，首次调用时懒启动客户端连接
- `_retrieve_knowledge()`：⚠️ **TODO 占位**，当前返回空引用（RAG 知识库检索未实现）
- `get_messages` / `list_conversations`：⚠️ **TODO 占位**，尚未接通 PostgreSQL

### 4.3 前端联调时需注意

| 接口 | 当前状态 | 前端表现 |
| ---- | -------- | -------- |
| 验证码/注册/登录/刷新/登出 | ✅ 真实可用 | 正常联调 |
| 发送消息 | ✅ 已接通真实 LLM | `data.content` 返回大模型真实回复 |
| 获取历史/会话列表 | ⚠️ 占位 | 返回空数组 |
| 创建会话 | ⚠️ 占位 | 返回固定 conversation_id |

> 注意：`send_message` 内部会先保存用户消息到 Redis 上下文（`append_context`），再调用 LLM。若 Redis 未启动或 LLM API Key 无效，接口会抛出异常，需在后端日志查看具体报错。

---

## 5. 本地协同（PC 端本地加速）接口

**`POST /local/sync-summary`**（需认证）— 源码 `app/api/v1/local.py`

用途：游戏模式等场景下，PC 端本地已生成对话，仅同步摘要到云端。

请求体：

```json
{
  "summaries": [
    { "conversation_id": "uuid", "content": "对话摘要", "scene": "game" }
  ]
}
```

响应：

```json
{ "code": 0, "message": "已同步 1 条摘要" }
```

---

## 6. 前端联调建议时序

```
1. GET  /auth/captcha                     → 展示验证码
2. POST /auth/login                       → 拿到 access_token / refresh_token
3. GET  /conversations/scenes             → 获取场景列表
4. POST /conversations                    → 创建会话（当前返回占位 id）
5. POST /conversations/{id}/messages      → 发送用户消息，data.content 即大模型回复，渲染到聊天界面
6. （access_token 过期时）
   POST /auth/refresh                     → 换新对，更新本地存储
7. 聊天记录拉取（待后端接通后可用）
   GET  /conversations/{id}/messages      → 加载历史消息
```

**前端核心对接点**：展示 AI 回复只认 `POST /conversations/{conversation_id}/messages` 响应的 `data.content` 字段；引用来源取 `data.citations`。

---

## 7. 附：统一错误码

| code | 含义 |
| ---- | ---- |
| 0 | 成功 |
| HTTP 400 | 参数错误 / 验证码错误 |
| HTTP 401 | 未登录或 token 失效 |
| HTTP 403 | 账号禁用 / 权限不足 |
| HTTP 409 | 账号已存在 |

非 0 业务码由各接口在 `message` 中给出具体原因。