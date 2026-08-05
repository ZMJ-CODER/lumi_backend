# 认证模块 API 文档

> Base URL: `http://localhost:8000/api/v1`
> 版本：1.0 | 最后更新：2026-08-01

---

## 安全架构概览

```
┌──────────────────────────────────────────────────────────────┐
│  客户端 (Electron / Web / 移动端)                              │
│  - 用户输入明文密码                                            │
│  - 通过 HTTPS POST 将明文密码发送至服务端                       │
│  - 客户端不缓存、不记录明文密码                                 │
│  - 客户端不执行任何哈希逻辑（无平台兼容性问题）                  │
├──────────────────────────────────────────────────────────────┤
│  传输层 (HTTPS)                                               │
│  - 全链路 TLS 加密，防止中间人窃听                             │
│  - 明文密码仅在本次请求体中短暂存在                             │
├──────────────────────────────────────────────────────────────┤
│  服务端 (FastAPI)                                             │
│  - password_hash = argon2id(password, salt, options)          │
│  - 每用户独立 16 字节随机盐（crypto 安全随机数）                │
│  - 数据库 users.password_hash 仅存储 PHC 格式哈希字符串         │
│  - 服务端绝不存储明文密码                                       │
└──────────────────────────────────────────────────────────────┘
```

**关键规则**：
- 客户端**直接发送明文密码**（依赖 HTTPS 保护传输），无需任何哈希库
- 服务端**绝不存储明文密码**，仅存储 argon2id 哈希值
- 所有密码哈希/校验在服务端完成，客户端无权参与
- 符合 OWASP 推荐：用户密码应在服务端使用自适应哈希算法存储

### 服务端 argon2id 参数

| 参数 | 值 | 说明 |
|---|---|---|
| memory_cost | 65536 KB | 64 MB 内存硬哈希，抗 GPU/ASIC |
| time_cost | 3 | 3 次迭代 |
| parallelism | 4 | 4 并行度 |
| hash_len | 32 | 32 字节哈希输出 |
| salt_len | 16 | 16 字节随机盐（≥128 位） |

存储格式（PHC）：`$argon2id$v=19$m=65536,t=3,p=4$<salt>$<hash>`

---

## 1. 图形验证码

```
GET /auth/captcha
```

**说明**：获取算式图形验证码（PNG Base64），用户需输入计算结果。

**安全策略**：
- 同一 IP 每分钟最多获取 10 次验证码
- 连续输错验证码 5 次，锁定该 IP 30 分钟

**响应**：
```json
{
  "code": 0,
  "data": {
    "captcha_id": "c7f1a2b3-...",
    "image_base64": "data:image/png;base64,..."
  }
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| captcha_id | string | 验证码唯一 ID，注册/登录时回传 |
| image_base64 | string | PNG 图片 Base64（含 data URI 前缀） |

**错误码**：
| HTTP | 说明 |
|---|---|
| 429 | IP 获取频率超限或已被锁定 |

---

## 2. 注册

```
POST /auth/register
```

**前置步骤**：
1. 调用 `GET /auth/captcha` 获取图形验证码
2. 用户输入账号、密码、确认密码、验证码结果

**请求体**：
```json
{
  "account": "user@example.com",
  "password": "myPassword123",
  "captcha_id": "c7f1a2b3-...",
  "captcha_result": "15"
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| account | string | ✅ | 账号（邮箱/手机号） |
| password | string | ✅ | 明文密码（HTTPS 保护），8-128 位，含字母和数字 |
| captcha_id | string | ✅ | 验证码 ID |
| captcha_result | string | ✅ | 验证码计算结果 |

**服务端处理流程**：
1. 全局认证限流（单 IP 每分钟 20 次）
2. 校验图形验证码（一次性，立即失效）
3. 检查账号唯一性
4. 密码强度检查（最少 8 位，包含字母和数字）
5. 生成 16 字节随机盐，调用 argon2id 生成密码哈希
6. 存储账号、哈希字符串、用户角色（默认 user）、创建时间
7. 注册即登录：签发 JWT + refresh_token

**响应**：
```json
{
  "code": 0,
  "message": "注册成功",
  "data": {
    "user_id": "550e8400-e29b-41d4-a716-446655440000",
    "username": "user@example.com",
    "access_token": "eyJhbGciOiJIUzI1NiIs...",
    "refresh_token": "a1b2c3d4e5f6...",
    "expires_in": 3600,
    "user": {
      "user_id": "550e8400-e29b-41d4-a716-446655440000",
      "username": "user@example.com",
      "avatar_url": "",
      "role": "user"
    }
  }
}
```

**错误码**：
| HTTP | 说明 |
|---|---|
| 400 | 验证码错误 / 密码强度不足 |
| 409 | 账号已存在 |
| 429 | 认证请求过于频繁 |

---

## 3. 登录

```
POST /auth/login
```

**前置步骤**：
1. 调用 `GET /auth/captcha` 获取图形验证码
2. 用户输入账号、密码、验证码结果

**请求体**：
```json
{
  "account": "user@example.com",
  "password": "myPassword123",
  "captcha_id": "c7f1a2b3-...",
  "captcha_result": "15"
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| account | string | ✅ | 账号（邮箱/手机号） |
| password | string | ✅ | 明文密码（HTTPS 保护） |
| captcha_id | string | ✅ | 验证码 ID |
| captcha_result | string | ✅ | 验证码计算结果 |

**服务端处理流程**：
1. 全局认证限流（单 IP 每分钟 20 次）
2. 账号锁定检查（连续失败 5 次锁定 15 分钟）
3. 校验图形验证码
4. 查询用户记录，提取存储的哈希字符串
5. 调用 argon2 验证函数比对密码
6. 验证成功，生成 JWT（载荷含 user_id、username、role）
7. 生成随机 refresh_token，其 SHA-256 哈希存入数据库，原值返回客户端
8. 记录登录日志（IP、时间戳）

**响应**：
```json
{
  "code": 0,
  "data": {
    "access_token": "eyJhbGciOiJIUzI1NiIs...",
    "refresh_token": "a1b2c3d4e5f6...",
    "expires_in": 3600,
    "user": {
      "user_id": "550e8400-e29b-41d4-a716-446655440000",
      "username": "user@example.com",
      "avatar_url": "",
      "role": "user"
    }
  }
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| access_token | string | 短期 JWT，1 小时有效，每次 API 请求携带 |
| refresh_token | string | 长期不透明令牌，30 天有效，用于刷新 access_token |
| expires_in | int | access_token 有效期（秒） |
| user.role | string | 角色：`user` / `admin` / `superadmin` |

**错误码**：
| HTTP | 说明 |
|---|---|
| 401 | 账号或密码错误 |
| 403 | 账号已被禁用 / 账号已被锁定 |
| 429 | 认证请求过于频繁 |

---

## 4. 刷新令牌

```
POST /auth/refresh
```

**请求体**：
```json
{
  "refresh_token": "a1b2c3d4e5f6..."
}
```

**说明**：使用 refresh_token 获取新的令牌对。采用**轮换策略**：验证旧 token → 废弃 → 签发新对，防止令牌重放。

**响应**：与登录相同，返回全新的 token 对。

```json
{
  "code": 0,
  "data": {
    "access_token": "eyJ...（全新）",
    "refresh_token": "f6e5d4c3b2a1...（全新，重新计时30天）",
    "expires_in": 3600,
    "user": { ... }
  }
}
```

**Token 滑动过期策略**：
- `access_token`：1 小时过期
- `refresh_token`：30 天过期，**每次刷新重新计时**
- 连续 30 天不登录 → 需要重新输入密码
- 30 天内任意时间登录/刷新 → 自动续期

**错误码**：
| HTTP | 说明 |
|---|---|
| 401 | 刷新令牌无效或已过期 |
| 403 | 用户不可用（已禁用） |

---

## 5. 登出

```
POST /auth/logout
```

**请求体**：
```json
{
  "refresh_token": "a1b2c3d4e5f6..."
}
```

**说明**：废弃 refresh_token（从数据库删除其哈希记录）。

**响应**：
```json
{
  "code": 0,
  "message": "已登出"
}
```

---

## 6. 获取当前用户信息

```
GET /user/me
Authorization: Bearer <access_token>
```

**响应**：
```json
{
  "code": 0,
  "data": {
    "user_id": "550e8400-e29b-41d4-a716-446655440000",
    "username": "user@example.com",
    "account": "user@example.com",
    "avatar_url": "",
    "role": "user",
    "status": "active",
    "created_at": "2026-08-01T12:00:00+00:00"
  }
}
```

**错误码**：
| HTTP | 说明 |
|---|---|
| 401 | 未登录或令牌无效 |
| 403 | 账号已被禁用 |
| 404 | 用户不存在 |

---

## 跨平台客户端实现指南

### 完整注册流程

```
1. GET  /auth/captcha           → 获取 captcha_id + image_base64
2. 用户在 UI 输入账号、密码、确认密码、验证码结果
3. POST /auth/register          → 提交 {account, password, captcha_id, captcha_result}
4. 收到成功响应后，保存令牌
```

### 完整登录流程

```
1. GET  /auth/captcha           → 获取 captcha_id + image_base64
2. 用户在 UI 输入账号、密码、验证码结果
3. POST /auth/login             → 提交 {account, password, captcha_id, captcha_result}
4. 保存 access_token + refresh_token
```

### 令牌存储（各平台）

| 平台 | 存储方式 |
|---|---|
| Electron 桌面端 | safeStorage 加密后存本地文件 |
| Web 浏览器 | httpOnly Cookie（推荐，防 XSS）或 localStorage（配合严格 CSP） |
| 移动端 (iOS/Android) | Keychain / EncryptedSharedPreferences |

### Token 使用规范

```
所有需要认证的 API 请求：
  Authorization: Bearer <access_token>

access_token 过期后：
  用 refresh_token 调用 POST /auth/refresh 获取新 token 对

refresh_token 也过期：
  跳转登录页，重新输入密码
```

### JWT 载荷结构

```json
{
  "sub": "550e8400-e29b-41d4-a716-446655440000",
  "username": "user@example.com",
  "role": "user",
  "type": "access",
  "iat": 1690000000,
  "exp": 1690003600
}
```

| 字段 | 说明 |
|---|---|
| sub | 用户 UUID |
| username | 用户昵称 |
| role | user / admin / superadmin |
| type | 固定 "access" |
| iat | 签发时间戳 |
| exp | 过期时间戳 |

---

## 安全策略汇总

### 密码安全
- 最少 8 位，必须包含字母和数字（可配置）
- 服务端 argon2id 加盐哈希（内存硬哈希，抗 GPU/ASIC）
- 每用户独立 16 字节随机盐，防彩虹表

### 暴力破解防护
| 策略 | 阈值 | 锁定时间 |
|---|---|---|
| 验证码获取限流 | 单 IP 10 次/分钟 | - |
| 验证码连续错误 | 5 次 | 锁定 IP 30 分钟 |
| 登录失败锁定 | 单账号 5 次 | 锁定 15 分钟 |
| 全局认证限流 | 单 IP 20 次/分钟 | - |

### 令牌安全
- JWT 使用 HS256 签名，密钥定期轮换
- refresh_token 仅存 SHA-256 哈希值，避免数据库泄露导致令牌可重用
- refresh_token 轮换策略：每次刷新废弃旧 token，防重放

### 传输安全
- 全站 HTTPS，开启 HSTS
- 明文密码仅在 HTTPS 请求体中短暂存在，客户端不缓存、不记录

### 敏感操作二次验证
- 修改密码、注销账号等需要重新输入密码

---

## 安全注意事项

1. **HTTPS 必须**：生产环境必须使用 HTTPS，密码明文依赖传输层加密
2. **客户端不哈希**：所有平台均无需引入密码哈希库，仅需标准 HTTPS
3. **token 安全存储**：按平台使用系统安全存储，避免明文落盘
4. **captcha 防重放**：每个 captcha_id 仅可使用一次
5. **明文密码不落地**：客户端使用后立即从内存中清除