# Locust 并发测试文档

项目已加入独立压测脚本：`loadtests/locustfile.py`。脚本默认只请求健康检查，不调用 LLM、不创建任务、不写入会话数据。

## 1. 本地确认 Locust

Windows 虚拟环境：

```powershell
.venv\Scripts\locust.exe --version
```

如果命令不存在，可先同步依赖：

```powershell
uv sync --extra dev
```

## 2. 首轮健康检查并发

先启动 API 容器后，在项目根目录运行：

```powershell
$null = New-Item -ItemType Directory -Force artifacts
$env:LUMI_LOAD_SCENARIO = "health"
.venv\Scripts\locust.exe -f loadtests\locustfile.py --headless `
  --host http://127.0.0.1:8000 `
  --users 20 --spawn-rate 5 --run-time 2m `
  --csv=artifacts\locust-health
```

重点观察 Locust 输出中的 RPS、平均延迟、p95/p99 和失败率。首次建议只用 20 用户；确认稳定后再测 50、100 用户。

## 2.1 本机受保护接口压测环境

普通接口受按 IP 的 300 次/分钟防刷限制保护。Locust 从单一 IP 高频发请求时，429 是安全机制生效，
不能作为应用性能失败解读。

本机可显式使用 `docker-compose.loadtest.yml` 临时关闭 API 的两层限流；该文件不会改 `.env`，也不需要重新构建镜像：

```powershell
docker compose -f docker-compose.yml -f docker-compose.loadtest.yml up -d --force-recreate api
```

压测完成后必须恢复正常配置：

```powershell
docker compose up -d --force-recreate api
```

`docker-compose.loadtest.yml` 禁止带入测试机以外的环境，更不能用于生产。

## 3. 已登录只读接口

需要一个有效的 access token。不要把 token 写进脚本或提交到 Git：

```powershell
$null = New-Item -ItemType Directory -Force artifacts
$env:LUMI_LOAD_SCENARIO = "protected"
$env:LUMI_LOAD_ACCESS_TOKEN = "<access_token>"
.venv\Scripts\locust.exe -f loadtests\locustfile.py --headless `
  --host http://127.0.0.1:8000 `
  --users 10 --spawn-rate 2 --run-time 2m `
  --csv=artifacts\locust-protected
```

该场景只测：`GET /api/v1/user/me`、`GET /api/v1/conversations`、`GET /api/v1/memory`。

## 3.1 单机最大 RPS 场景

`max_rps` 复用多用户只读接口，但虚拟用户请求完成后不等待，适合测服务端吞吐上限，
不代表真实用户访问节奏。必须使用压测覆盖层（限流关闭），并逐档增加用户数；每档至少运行 2 分钟，
一旦失败率持续上升或 p99 明显失控就停止。

```powershell
$env:LUMI_LOAD_SCENARIO = "max_rps"
$env:LUMI_LOAD_TOKENS_FILE = "$PWD\artifacts\loadtest-users.json"
.venv\Scripts\locust.exe -f loadtests\locustfile.py --headless `
  --host http://127.0.0.1:8000 `
  --users 200 --spawn-rate 50 --run-time 2m `
  --csv=artifacts\locust-max-rps-200
```

建议阶梯：`200 → 400 → 800 → 1200` 用户。每档记录稳定 RPS、失败率、p95/p99/p99.9、
API CPU/内存、PostgreSQL 连接数；把“失败率接近 0 且 p99 未持续恶化”的最高稳定 RPS 作为容量基线。

## 4. 消息链路（安全冒烟）

该场景要求一个已存在且属于该账号的会话 ID，并固定 `local_mode=true`，只验证会话锁、鉴权、幂等字段和请求链路，不触发真实模型：

```powershell
$env:LUMI_LOAD_SCENARIO = "local_message"
$env:LUMI_LOAD_ACCESS_TOKEN = "<access_token>"
$env:LUMI_LOAD_CONVERSATION_ID = "<conversation_uuid>"
.venv\Scripts\locust.exe -f loadtests\locustfile.py --headless `
  --host http://127.0.0.1:8000 `
  --users 5 --spawn-rate 1 --run-time 1m
```

真实聊天或办公 DAG 压测暂不默认加入脚本：它会消耗模型额度、占用 Celery/Redis/agent 租约并产生持久化数据。需要做这一档时，应先单独约定测试账号、模型额度、清理策略和并发上限。

## 4.1 多用户受保护读压测

单一 token 会让所有 Locust 用户共享一组 Redis key，只能代表单用户热缓存。使用下面的脚本直接在
数据库创建专用的 `loadtest-0001@loadtest.invalid` 至 `loadtest-0200@loadtest.invalid` 账号，并将短期
access token 写进被 Git 忽略的 `artifacts/loadtest-users.json`。它不调用注册接口，也不会读取、修改或
删除非 `@loadtest.invalid` 账号。

```powershell
.venv\Scripts\python.exe scripts\seed_loadtest_users.py --count 200
```

随后让每一个 Locust 虚拟用户分配不同 token：

```powershell
$env:LUMI_LOAD_SCENARIO = "protected"
$env:LUMI_LOAD_TOKENS_FILE = "$PWD\artifacts\loadtest-users.json"
.venv\Scripts\locust.exe -f loadtests\locustfile.py --headless `
  --host http://127.0.0.1:8000 `
  --users 200 --spawn-rate 10 --run-time 10m `
  --csv=artifacts\locust-protected-multi-user-200
```

使用多用户 token 时，缓存命中率会比单用户基线低，这是预期行为；重点比较失败率、p99、PG 连接数和
`db_checkout`，而不是要求 Redis CPU 很高。token 有效期与 `ACCESS_TOKEN_EXPIRE_SECONDS` 相同；过期后
重新运行种子脚本即可，脚本会复用已有专用账号并只更新本地 token 文件。

## 5. Docker 部署后再执行

本次只完成脚本和测试准备，不启动容器。容器部署并确认 API 健康后，直接执行第 2 节命令即可。若从另一台机器压测，把 `--host` 改为 API 的内网地址；不要把压测端口暴露到公网。

建议每次记录：提交版本、API worker 数、`DB_POOL_SIZE`/`DB_MAX_OVERFLOW`、Redis/PG 连接数、并发用户数、运行时长及失败响应样本。

## 6. 高频只读缓存与长尾复测

`/api/v1/user/me`、`/api/v1/conversations` 和 `/api/v1/memory` 使用独立的 Redis
只读视图缓存。它们分别采用 5、10、15 秒 TTL；缓存键带 user_id，分页参数也参与会话列表
键的哈希，因此不同用户或不同页不会互相看到数据。缓存不是授权来源：请求仍先验证 JWT；Redis
不可用、缓存内容损坏或关闭 `READ_VIEW_CACHE_ENABLED` 时，接口直接回退数据库。

会话创建、消息落库、重命名、删除会清理该用户的全部会话列表页；记忆抽取、画像重建、删除和
清空会清理记忆视图；个人资料、密码以及管理员修改角色或账号状态会清理 `/user/me` 视图。

代码变更后需要重新构建 API 镜像，再以压测覆盖层启动：

```powershell
docker compose -f docker-compose.yml -f docker-compose.loadtest.yml up -d --build --force-recreate api
```

先让缓存预热 15 秒，再用同一 token 对比缓存开关。200 并发、6 worker 的本机示例：

```powershell
$env:LUMI_LOAD_SCENARIO = "protected"
$env:LUMI_LOAD_ACCESS_TOKEN = "<access_token>"
.venv\Scripts\locust.exe -f loadtests\locustfile.py --headless `
  --host http://127.0.0.1:8000 `
  --users 200 --spawn-rate 20 --run-time 2m `
  --csv=artifacts\locust-read-cache-200
```

检查缓存而不是只看 Redis CPU：

```powershell
curl.exe -s http://127.0.0.1:8000/metrics | Select-String "lumi_read_view_(cache_total|stage_duration_seconds)"
```

预期是 `result="hit"` 明显高于 `miss`，且 `cache_get` 很短；缓存命中后不会出现
`db_checkout`/`sql` 样本。若 p99 仍高，按 `db_checkout`、`sql`、`response_build` 三段归因，
而不是盲目增加 Redis：前两段高分别代表连接池排队和查询问题，后段高才是序列化或应用 CPU 问题。

### HTTP 0 的判读

Locust 的 `HTTP 0` 不是 HTTP 响应码，表示客户端在收到响应前遇到传输层异常（例如连接重置、
空响应或客户端超时）。压测脚本会保留 `response.error`，因此应以 Error report 中的具体异常
为准；不要把 HTTP 0 自动重试，否则会掩盖服务端长尾。压测覆盖层已将 Uvicorn
`--timeout-keep-alive` 设为 30 秒；如果错误统一是 `RemoteDisconnected` 且延迟约 5~6 秒，
通常是客户端复用 Uvicorn 默认 5 秒回收的空闲连接，不是业务失败。出现 HTTP 0 时同时执行：

```powershell
docker compose logs --since 10m api | Select-String "Traceback|TooManyConnections|timeout|reset|broken pipe|ERROR"
docker stats --no-stream lumi-api lumi-postgres lumi-redis
docker exec lumi-postgres psql -U postgres -d lumi_db -c "select count(*) as connections from pg_stat_activity;"
```

若错误为 `ReadTimeout`，先检查客户端/反向代理超时；若为 `ConnectionResetError` 或
`RemoteDisconnected`，检查 API worker 是否重启、Docker Desktop 是否抖动；若日志出现
`TooManyConnectionsError`，按 `worker × (pool_size + max_overflow)` 重新下调连接池。5 秒级
少量长尾通常是缓存失效时的回源和进程调度，不应仅凭 Redis CPU 很低就继续扩容缓存。
