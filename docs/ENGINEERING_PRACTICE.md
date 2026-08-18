# Lumi 工程落地策略

> 版本：v1.0（2026-08-17）
> 主题：从"能跑"到"敢用"——异步化、可观测性、数据库治理、部署、工程质量、降级策略
> 数据来源：真实压测与线上修复记录，详见 `CONCURRENCY_PERF.md`

## 1. 落地原则

1. **渐进式**：先跑通最小链路，再按痛点逐层优化（OCR 阻塞 → 线程池 → 队列日志 → 多 worker）。
2. **可回退**：每项新能力都保留降级路径（Temporal → legacy、MCP → Redis 轮询、云 LLM → 本地）。
3. **可观测**：所有决策有日志、有指标、有审计；日志不能因为性能牺牲。
4. **可配置**：关键阈值全部收敛到 `app/core/config.py`（pydantic-settings + .env），
   改配置不碰代码。
5. **质量门禁**：CI 强制 ruff 0 错误 + pytest 全绿 + Alembic 迁移可执行。

## 2. 异步化改造：把 CPU 密集任务从事件循环里挪走

### 2.1 问题

OCR / Docling 解析 / Embedding / TTS 都是同步 CPU 密集调用。若在 async 接口里直接调用，
一个上传请求就会把整个事件循环卡死。实测：OCR 进行期间 `/api/v1/health` 延迟飙到
**3046ms**（正常 <30ms）。

### 2.2 修复演进

第一版：`asyncio.to_thread(...)` 直接切到默认线程池。

第二版（最终）：独立有界线程池 `app/core/executors.py`：

```python
_compute_pool = ThreadPoolExecutor(
    max_workers=max(2, settings.COMPUTE_THREADS),
    thread_name_prefix="compute",
)

async def run_in_compute(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_compute_pool, partial(fn, *args, **kwargs))
```

原因：`asyncio.to_thread` 复用默认线程池（约 CPU 核数 + 4），多人并发上传时计算任务会
占满默认池，把 DB 连接维护、FastAPI 同步依赖等后台任务一起饿死。独立池隔离配额，
互不干扰。

### 2.3 覆盖范围与约束

- 覆盖：OCR、Docling 文档解析、Embedding 推理、Whisper 转写、TTS 合成。
- 约束：`COMPUTE_THREADS` 默认 4（单人/小团队够用），多 worker 部署时每进程一份，
  GPU 模型会成倍占用显存，需按显存权衡 worker 数。

## 3. 日志与可观测性：先留日志，再谈性能

### 3.1 问题

压测发现 Windows 上 uvicorn 访问日志同步写 stderr（重定向到文件时串行阻塞事件循环），
health 并发 20 只有 344 RPS；关闭访问日志直接到 990~1145 RPS。这是**第一串行瓶颈**。

### 3.2 方案：QueueHandler + QueueListener

`app/core/logging.py` 的 `setup_uvicorn_queue_logging()`：

- uvicorn.access / uvicorn.error 切到 `QueueHandler`，请求路径只做一次内存入队（微秒级）；
- 独立 `QueueListener` 线程负责实际 I/O（队列上限 20000 防积压）。

A/B 实测（health）：

| 模式 | 并发 20 | 并发 50 |
| --- | --- | --- |
| 同步写访问日志 | 344 RPS / p50 56ms | 112 RPS / p50 295ms |
| QueueHandler 队列日志 | ~200 RPS / p50 57ms | ~290 RPS / p50 163ms |
| `--no-access-log` | 990~1145 RPS | 800~1010 RPS |

结论与生产建议：

- **Windows 上队列日志仍有 GIL/句柄竞争**，追不上 `--no-access-log`；默认保留队列日志保可观测性。
- **Linux 上 1 worker 队列日志即达 900~980 RPS**（≈ Windows 关日志水平且日志完整保留），
  说明这是 Windows 特有问题，容器部署自然消解。
- 极限吞吐场景用 `--no-access-log`，或把访问日志交给独立进程。

### 3.3 其他可观测性

- Sentry（`SENTRY_DSN`）错误上报，未配置不启用。
- Prometheus `/metrics`（`METRICS_ENABLED`），内置指标暴露接口。
- 审计日志 `control_logs`：谁、何时、哪个任务、做了什么写操作、依据是什么。

## 4. 数据库层治理

### 4.1 连接池

- 参数化：`DB_POOL_SIZE`（默认 10）/ `DB_MAX_OVERFLOW`（默认 20）→ 每进程最多 30 连接。
- 教训：原 `pool_size=20, max_overflow=10`，多 worker 部署后 Postgres 活跃连接达 89/100，
  连接排队导致 DB 接口吞吐骤降。
- 公式：`N worker × (DB_POOL_SIZE + DB_MAX_OVERFLOW) < Postgres max_connections`。
  默认 30 连接/进程时最多 3 worker 安全；更多 worker 需调大 DB 上限或调小池。

### 4.2 `DB_PRE_PING`

- 默认关闭：本地/局域网稳定环境省掉取连接时的 SELECT 1，压测约 +20% DB 吞吐。
- 公网/容器网络抖动环境建议开启，避免拿到失效连接。

### 4.3 health 接口 TTL 缓存

原 `/api/v1/health` 每次请求 SELECT 1 + Redis ping 并占一个池槽位，探活一多就打满连接池。
改为 TTL 缓存探测：正常 5s 刷新、异常 1s 刷新，其余请求直接返回缓存。
实测 health RPS 从 93 → 344 →（关日志）900+。

### 4.4 实测否决项（附证据）

- **NullPool 短连接**：本机并发 10 仅 11.7 RPS（asyncpg 在 Windows 建连成本高且串行），
  比池化（171 RPS）差 15 倍，**不采用**。
- **AUTOCOMMIT 只读会话**：并发 50 时 104 RPS vs 池化 95 RPS，收益有限且有事务语义风险，
  **未全局采用**。
- 瓶颈定位：Postgres 单连接 SELECT 1 仅 0.14ms，原生 asyncpg 预热并发 50 达 6800+ RPS；
  瓶颈在 SQLAlchemy `AsyncAdaptedQueuePool` 的取连接/归还锁（Windows 尤其明显）。
  结论：**单进程内无法再大幅优化 DB 并发，扩展手段就是多 worker。**

### 4.5 pgvector 迁移

`alembic/versions/0001_baseline.py` 顶部加了 `CREATE EXTENSION IF NOT EXISTS vector`，
解决全新库跑基线迁移时找不到 vector 类型的失败。

## 5. 多 worker 扩展与部署

### 5.1 压测结论（Windows → Linux Docker）

| 场景 | Windows 单进程 | Linux 1 worker | Linux 4 worker |
| --- | --- | --- | --- |
| health 并发 20 | ~200 RPS | **982 RPS** | **1467 RPS** |
| health 并发 100 | — | 882 RPS | **1382 RPS** |
| /user/me 并发 50 | 28~69 RPS | 59 RPS（p50 620ms） | **79 RPS（p50 118ms）** |

- Linux 解决了 Windows 特有问题（日志/句柄竞争），多 worker 在 Linux 同样线性扩展。
- DB 接口瓶颈跨平台存在，靠多 worker 缓解；单机/小团队单进程足够。
- 设备资源占用低是正常现象：架构是"延迟受限"而非"吞吐受限"。

### 5.2 Docker 镜像

- Dockerfile 支持 CUDA torch：`ENABLE_CUDA_TORCH` 默认 true，`TORCH_INDEX_URL` 默认
  pytorch cu126；Linux 上解析不到 +cu126 wheel 时自动走 CPU 版（CI 可解析）。
- **镜像体积告警**：约 20GB（torch 2.13 + 模型依赖），生产可接受但需注意拉取/构建时间；
  可用 BuildKit cache mount（`--mount=type=cache,target=/root/.cache/pip`）复用本地缓存。
- GPU 使用：容器内 CUDA 可用（RTX 4060 验证通过）；多 worker 每份模型成倍占显存。
- 容器连宿主机 Postgres/Redis：`host.docker.internal`，单查询约 1ms 跳转，占比不大。

### 5.3 依赖解析踩坑

- `pyproject.toml` 里 `[tool.uv.sources]` 仅 Windows 指向阿里云 CUDA wheel
  （`marker = "sys_platform == 'win32'"`）；Linux/CI 走公开索引 CPU 版，保证
  `uv sync --no-sources` 可解析。
- `[tool.uv].environments` 限定 `linux` + `win32`，跳过 macOS 解析，避免因 +cu126
  没有 darwin wheel 导致全平台锁定失败。
- 不强制默认索引为国内镜像：CI 访问清华源会 403，本机通过环境变量
  `UV_DEFAULT_INDEX` 覆盖。

## 6. 工程质量：ruff 与 pytest 治理

### 6.1 ruff 收敛（239 → 0）

- 规则收敛到正确性类：`E F W B`（去掉 I/N/UP/SIM 等大规模重构类），忽略 E501 行长噪音。
- FastAPI 声明式调用（Depends/File/Query/Header 等）通过
  `flake8-bugbear.extend-immutable-calls` 豁免，避免几百处误报。
- 修复了 4 个 F821 未定义名、25 个 B904（补 `from`）、Temporal lambda 变量捕获
  （显式绑定，避免循环变量闭包错误）。
- `__init__.py` 的 F401 用 per-file-ignores 处理（替代已废弃的
  `ignore-init-module-imports`）。

### 6.2 pytest 治理（70 passed）

- `addopts = "-p no:cacheprovider"`：屏蔽 Windows 上 `.pytest_cache` 权限异常
  （WinError 5）导致的 PytestCacheWarning。
- `filterwarnings` 屏蔽 jieba 等第三方库的无效转义 SyntaxWarning（本项目代码的
  转义问题由 ruff W605 把关）。
- 补齐 `striprtf` 依赖（RTF 伪装 .doc 解析用），pyproject + uv.lock 同步更新。

### 6.3 CI 门禁（GitHub Actions）

流水线依次执行：`uv sync --no-sources` → `alembic upgrade head` → `ruff check` →
`pytest`。三者全绿才算通过；迁移在全新库上跑通，避免"本地能跑、CI 挂"。

## 7. 失败与降级策略

| 场景 | 降级路径 | 依据 |
| --- | --- | --- |
| Temporal 不可用 | 自动回退自建 DAG（legacy 引擎） | `AGENT_ORCHESTRATION` |
| MCP 客户端不可用 | 失败冷却 30s + Redis 轮询通道 | `app/agents/mcp/manager.py` |
| 主 LLM 供应商失败 | 自动切换备用供应商（qwen ↔ deepseek） | `LLM_FALLBACK_PROVIDER` |
| Redis 限流故障 | 放行（防击穿） | 限流中间件 |
| 规划器未覆盖文档 | 强制补 `office_doc` 分析节点 | 编排层兜底 |
| 节点执行失败 | 重试 ≤2 次，超时 5 分钟 | `AGENT_NODE_MAX_RETRIES` |
| 脚本产物 | 输出目录 7 天 TTL 自动清理；沙箱临时目录 6h 兜底 | `GENERATED_FILES_TTL_DAYS` |

## 8. 安全落地

- 密码：argon2id（每用户独立盐），服务端存储；8 位 + 字母 + 数字策略。
- JWT：短期 access token（1h）+ 长期 refresh token（30 天滑动刷新）。
- 认证防爆破：验证码/登录连续失败锁定 IP 或账号（次数与时长可配置）。
- Docker secret：敏感字段（JWT 密钥/记忆密钥/LLM Key）支持从 `/run/secrets` 注入，
  环境变量为默认来源，secret 文件优先覆盖。
- 写操作分级：只读自动执行；低风险写自动执行留审计；高风险（发邮件/改主数据/付款）
  挂人工审批门控。
- 隐私：精确 PII 不进入长期记忆；私密信息加密落库，按需解密并审计。

## 9. 下一步（按优先级）

1. 早晚报/待办定时触发（Celery beat 已就绪，补模板 DAG 定时入口）。
2. 邮件真实发送（SMTP/IMAP 授权 + 审批门控）。
3. PDF 结构化编辑（Docling 结构回写）。
4. 会话/知识库跨端迁移，多端体验一致。
5. 大规模场景：读写分离、Kafka 削峰、分片检索。
