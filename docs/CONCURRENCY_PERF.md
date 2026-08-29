# 并发与性能测试报告（历史快照）

> 测试日期：2026-08-17 ｜ 环境：Windows + 单机（PostgreSQL 18 / Redis / GPU）
> 工具：aiohttp 异步压测（本机 localhost）
>
> 本文是 2026-08-17 的基准记录，不是当前生产 SLA 或容量承诺。代码、硬件、容器配置和模型供应商变化后，
> 必须按相同脚本重新测量；当前默认编排运行时为持久化 asyncio DAG，Temporal 仅灰度静态只读任务。

## 一、结论速览

| 项 | 修复前（单进程） | 修复后（单进程） | 4 worker |
| --- | --- | --- | --- |
| `/api/v1/health` RPS（并发 20） | 93 | 344 | — |
| `/api/v1/health` p95（并发 20） | 556ms | 63ms | — |
| `/api/v1/health` RPS（并发 100） | 107（p99 3.2s） | 327（p99 0.47s） | 1320（p99 0.11s） |
| `/api/v1/user/me` RPS（并发 50） | ~29（受连接池打满拖累） | ~28（池 10+20） | 91 |
| OCR 上传期间 health 最大延迟 | **3046ms**（事件循环被阻塞） | 28ms | — |

结论：**单进程足以支撑单人/小团队使用**（轻量接口 300+ RPS，DB 接口 30~90 RPS）；
要上高并发，用多 uvicorn worker 线性扩展（4 worker 下 health 1300+ RPS、DB 接口约 90 RPS），
关键前提是连接池总数不能超过 Postgres `max_connections`。

## 二、为什么设备资源占用不高但吞吐封顶

测试时 CPU/GPU/内存占用都很低，说明瓶颈不是算力而是**串行等待**。
逐层定位出两条真正的串行瓶颈：

### 1. uvicorn 访问日志写入（已实测，影响最大）
压测时每个请求都写一行访问日志到文件（`> log 2>&1` 重定向），
Windows 文件写入是串行的，直接卡住事件循环：

| health（并发 20） | 开访问日志 | 关访问日志 |
| --- | --- | --- |
| RPS | 344 | **1145** |
| p50 | 56ms | 17ms |

生产建议：高 QPS 部署加 `--no-access-log`，或把访问日志交给独立进程/只记错误。

### 2. SQLAlchemy 异步会话层的并发上限（当前剩余瓶颈）
独立基准（无 uvicorn）：

| 层 | 并发 5 | 并发 50 |
| --- | --- | --- |
| 原生 asyncpg 池（预热） | 5096 RPS | 6839 RPS |
| SQLAlchemy 异步会话 + SELECT 1 | 479 RPS | 108 RPS |

Postgres 单连接 `SELECT 1` 仅 0.14ms，DB 本身飞快；
瓶颈在 SQLAlchemy `AsyncAdaptedQueuePool` 的取连接/归还锁在 Windows 上串行化。
这解释了：DB 接口单进程封顶约 30~90 RPS，CPU 却上不去。

**结论：单进程内无法再大幅优化 DB 并发，扩展手段就是多 worker。**
4 worker 下 `/user/me` 并发 50 从 28 → 91 RPS。

### 3. 小优化（已落地）
- `DB_PRE_PING`（默认关）：去掉取连接时的 SELECT 1，约 +20% DB 吞吐；
  公网/容器网络抖动环境可开。
- 事件循环：Windows Proactor / Selector 实测无差异，不必改。

## 三、第三轮：队列化日志 + 专用计算线程池（2026-08-17 追加）

### 1. 访问日志 QueueHandler（已落地）
`app/core/logging.py` 新增 `setup_uvicorn_queue_logging()`：uvicorn.access/error 日志
切到 QueueHandler + QueueListener 后台线程写 stderr，事件循环只做一次内存入队。
已接入 `main.py` 生命周期，开机即生效。

A/B 实测（health，并发 20/50）：

| 模式 | 并发 20 | 并发 50 |
| --- | --- | --- |
| 同步写访问日志 | 344 RPS / p50 56ms | 112 RPS / p50 295ms |
| QueueHandler 队列日志 | ~200 RPS / p50 57ms | ~290 RPS / p50 163ms |
| `--no-access-log` | 990~1145 RPS | 800~1010 RPS |

结论：队列日志比同步日志稳定（不阻塞事件循环），但 Windows 上后台线程写文件仍有
GIL/句柄竞争，追不上 `--no-access-log`。**默认保留队列日志（保可观测性）；
追求极限吞吐时用 `--no-access-log`。**

### 2. 计算任务专用线程池（已落地）
`app/core/executors.py` 提供 `COMPUTE_THREADS`（默认 4）的独立线程池，
OCR / Docling / Embedding / Whisper / TTS 全部从默认 `asyncio.to_thread` 切换到
`run_in_compute`，避免并发上传占满默认线程池（CPU 核数+4）饿死 Web 后台任务。

### 3. 方案验证结果（未采纳项，附证据）
- **NullPool 短连接**：实测本机并发 10 只有 **11.7 RPS**（asyncpg 在 Windows 上
  建连成本高且串行），比池化（171 RPS）差 15 倍。**不采用**。
- **AUTOCOMMIT 只读会话**：并发 50 时 104 RPS vs 池化 95 RPS、p50 更低，
  收益有限且有事务语义风险（全局会话可能写），**未全局采用**；高频只读接口
  后续可按需局部使用。

## 四、第四轮：Linux Docker 容器对比（2026-08-17）

环境：Docker Desktop (WSL2) + RTX 4060（容器内 CUDA 可用），连宿主机
Postgres/Redis（host.docker.internal），与 Windows 同脚本、同并发压测。

| 场景 | Windows 单进程（队列日志） | Linux 1 worker | Linux 4 worker |
| --- | --- | --- | --- |
| health 并发 20 | ~200 RPS | **982 RPS** | **1467 RPS** |
| health 并发 50 | ~290 RPS | **970 RPS** | **1206 RPS** |
| health 并发 100 | — | 882 RPS | **1382 RPS** |
| /user/me 并发 10 | 76 RPS | 76 RPS | **95 RPS** |
| /user/me 并发 50 | 28~69 RPS | 59 RPS（p50 620ms） | **79 RPS（p50 118ms）** |

结论：
1. **Linux 解决了"队列日志仍拖慢事件循环"的 Windows 特有问题**：1 worker 下
   health 达到 900~980 RPS（≈ Windows 关访问日志的水平，且日志完整保留）。
2. **多 worker 在 Linux 同样线性扩展**：4 worker health 并发 100 → 1382 RPS。
3. **DB 接口瓶颈跨平台存在**：/user/me 单进程 59~76 RPS、4 worker 79~95 RPS——
   SQLAlchemy 异步连接池锁竞争在 Linux 依旧，不是 Windows 专属；并发大时建议多 worker。
4. 容器到宿主机 DB 单查询约 1ms（host.docker.internal 跳转），占比不大。

## 二、发现并修复的问题

### 1. OCR / Docling 同步阻塞事件循环（最严重）
`read_structure` / `extract_full_text` 是 CPU 密集同步操作（图片 OCR、PDF 解析），
原先在 async 接口里直接调用，一个上传就把整个事件循环卡死（实测 health 延迟飙到 3 秒）。

修复：`app/api/v1/office_docs.py`、`app/services/office_docs.py`、
`plugins/skills/office/office_docs.py` 全部改为 `asyncio.to_thread(...)`。

### 2. health 接口每次占用数据库连接池
原 `/api/v1/health` 每次请求都 `SELECT 1` + Redis ping 并占用一个池槽位，
探活请求一多就把连接池打满。

修复：`app/api/v1/health.py` 改为 TTL 缓存探测（正常 5s / 异常 1s 刷新），
其余请求直接返回缓存，不碰连接池。

### 3. 连接池过大打爆 Postgres 连接数
原 `pool_size=20, max_overflow=10`，多 worker 部署时每进程 30 连接，
实测两个实例 3 个 worker 后 Postgres 活跃连接达 89/100，连接排队导致 DB 接口吞吐骤降。

修复：`app/core/config.py` 新增 `DB_POOL_SIZE`（默认 10）/ `DB_MAX_OVERFLOW`（默认 20），
`app/core/database.py` 读取配置。默认 30 连接/进程：单进程充足，2~3 worker 也不会打爆 100 上限。

## 三、部署建议（高并发）

1. 多 worker：`uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4`
2. 连接池与数据库匹配：`N 个 worker × (DB_POOL_SIZE + DB_MAX_OVERFLOW) < Postgres max_connections`。
   默认 10+20 时最多 3 worker 安全；更多 worker 请调大 Postgres `max_connections` 或调小池。
3. 保持限流开启（`RATE_LIMIT_ENABLED=true`，默认）；压测时用 `RATE_LIMIT_ENABLED=false` 临时关闭。
4. GPU 模型（OCR / Embedding）每 worker 一份，多 worker 会成倍占用显存，按需权衡。
5. 单机 Postgres 建议 `shared_buffers=256MB+`、`max_connections=200+`（生产）。
6. 访问日志：`--no-access-log` 或独立日志进程（见上）。
7. 设备资源低是正常现象：本架构是"延迟受限"而非"吞吐受限"，瓶颈在事件循环等待与
   连接池锁，不在 CPU/GPU/内存。

### 高频读取缓存（2026-08-28）

受保护的只读接口在重复轮询时会放大连接池排队：`/user/me`、`/conversations`、`/memory`
都需要用户隔离，但返回数据在数秒内通常不变。因此新增 `app/core/read_view_cache.py`：

- 缓存键为 `api:view:{kind}:{user_id}`；会话列表额外哈希 `scene/limit/offset`，不共享跨用户或跨页响应。
- TTL 为 `user=5s`、`conversations=10s`、`memory=15s`。Redis 仅作优化，任何连接、序列化或缓存值错误均 fail-open 回退 PostgreSQL。
- 每个写路径主动失效：会话及消息写入清理全部该用户会话页，记忆和画像写入清理记忆视图，用户资料/角色/状态变化清理用户视图。
- `/metrics` 同时暴露 `lumi_read_view_cache_total`（hit/miss/error/store）和
  `lumi_read_view_stage_duration_seconds`（`cache_get`、`db_checkout`、`sql`、`response_build`）。

这不是替代数据库优化。只应在相同 token、相同列表页的热读压测中期待明显收益；首个请求、
缓存失效后的请求和实际写入仍走数据库。长尾应首先根据分段指标判断是连接池、SQL 还是应用
CPU，再决定是否扩容 worker、改索引或优化响应体。

### 外部依赖与办公任务保护（2026-08-19）

- **外部依赖熔断**：LLM（按 `base_url + model` 隔离）、Tavily 与 MCP 在连续 5 次
  网络/超时/429/5xx 失败后熔断 30 秒，半开仅放行一个探测调用。401、402、模型名错误等
  用户配置问题不计入熔断；LLM 主链路熔断时仍可走已配置的备用供应商。
- **精细令牌桶**：保留 IP 固定窗口作为最外层防刷，并新增按用户（游客按 IP）的令牌桶：
  聊天流、办公提交、上传分组独立；`Retry-After` 会随 429 一起返回。SSE 仅在建连时消费一次。
- **办公背压**：规划前最多允许 `AGENT_SUBMISSION_MAX_INFLIGHT` 个请求占用短槽位，
  校验通过后提升为全局/用户活跃槽位。超过 `AGENT_GLOBAL_ACTIVE_JOB_LIMIT` 或用户上限会立即
  返回 429，而非堆积在 API 内存；完成、失败、取消与投递失败均释放槽位，Redis TTL 负责宕机兜底。

### 容器部署前检查

主镜像会执行 `pip install .` 并在构建期导入 `temporalio`、`striprtf` 和应用入口；因此依赖清单或 wheel
不完整会在镜像构建期失败，而不是首次请求才暴露。脚本沙箱镜像也会导入 `openpyxl`、`python-docx`、
`python-pptx`。

构建并启动前执行：

```bash
export DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)
docker compose --profile sandbox build sandbox-image
docker compose up -d --build
```

挂载 Docker socket 等同宿主机高权限，只适用于受信任的自托管部署。公网多租户部署应将脚本沙箱替换为独立
服务，而不是向 API 容器授予 Docker socket。

## 四、测试方法（可复现）

- 登录：取验证码 → 读 Redis 答案 → POST /api/v1/auth/login（测试账号）。
- 压测：aiohttp 并发脚本，分别测 `/api/v1/health`（无 DB）与 `/api/v1/user/me`（DB 查询），
  并发 1/20/50/100，统计 RPS 与 p50/p95/p99。
- 阻塞验证：上传大图触发 OCR 的同时连续 ping `/api/v1/health`，比较修复前后最大延迟。
