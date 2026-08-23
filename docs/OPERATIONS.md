# 运维手册（Ops）

## 1. 部署启动（Docker）

```bash
# 基础启动（含 postgres / redis / api / worker / beat / tts）
docker compose up -d

# 用 Docker secrets 注入密钥（推荐生产）
docker compose -f docker-compose.yml -f docker-compose.secrets.yml up -d
```

`migrate` 是唯一执行 `alembic upgrade head` 的一次性部署服务；`api` 副本不会执行 DDL，
可安全横向扩容。先确认 `lumi-migrate` 以退出码 0 结束，再检查 API 健康状态。

异步任务已按 `durable`、`best_effort`、`maintenance` 三个 worker 隔离；本地 Compose
应整体启动，避免记忆/维护队列无人消费：

```powershell
docker compose up -d --build
```

分派契约、Celery 恢复语义和 `/metrics` 指标见 [ASYNC_TASK_DISPATCH.md](ASYNC_TASK_DISPATCH.md)。

## 2. 定时任务（Celery Beat）

`beat` 服务负责定时调度，已配置：

| 任务 | 时间 |
| --- | --- |
| 每日 token 用量聚合 | 02:00 |
| 聊天记录超限裁剪 | 03:00 |
| 长期记忆清理 | 03:30 |
| 用户画像重建 | 04:00 |

确认在跑：`docker compose ps` 中 `lumi-beat` 状态为 Up。

## 3. 数据库迁移（Alembic）

```bash
# 升级到最新
python -m alembic upgrade head

# 模型变更后生成迁移
python -m alembic revision --autogenerate -m "describe change"
python -m alembic upgrade head
```

迁移记录表为 `alembic_version`。应用进程不执行运行时 DDL；所有模式变更必须通过新的
Alembic revision 发布。

## 4. 密钥管理

### 环境变量（开发）
复制 `.env.example` 为 `.env` 并填写真实值。

### Docker secrets（生产）
见 `docker-compose.secrets.yml` 顶部注释。文件挂载到 `/run/secrets/<字段名>`，
后端启动时自动读取并覆盖环境变量（支持：`JWT_SECRET_KEY`、`MEMORY_ENCRYPTION_KEY`、
`DEEPSEEK_API_KEY`、`QWEN_API_KEY`、`TAVILY_API_KEY`）。

### 密钥轮换
- **JWT / API Key**：更新 `secrets/` 或 `.env` 后重启容器即可（JWT 轮换会使旧 token 失效）。
- **记忆主密钥**：执行
  ```bash
  export NEW_MEMORY_ENCRYPTION_KEY="<openssl rand -base64 32>"
  export NEW_MEMORY_ENCRYPTION_KEY_VERSION=2
  python scripts/rotate_memory_key.py
  ```
  脚本会用新密钥重加密全部密文并升级 `key_version`，完成后更新 `.env`/secrets 再重启。

## 5. 数据备份与恢复

```bash
# 数据库备份（保留最近 14 份）
bash scripts/backup_db.sh
# 建议 cron：0 3 * * * cd /path/to/lumi_backend && bash scripts/backup_db.sh

# 恢复
docker compose exec postgres psql -U postgres -d lumi_db < backup.sql
```

注意：`data/`（上传文件、office 会话、模型缓存）也要纳入备份/卷快照。

## 6. 可观测性

- **健康检查**：`GET /api/v1/health`
- **Prometheus 指标**：`GET /metrics`（HTTP 请求量/延迟、任务结果、技能调用、RAG 命中）
- **错误上报**：`.env` 配置 `SENTRY_DSN` 后自动上报未处理异常
- **日志**：`logs/lumi_*.log`（按天轮转、30 天保留）

### 本地开发产物清理

以下内容不参与运行时状态、不应提交 Git，可在停止本地开发进程后删除：

```powershell
Remove-Item -Recurse -Force .pytest_tmp, .ruff_cache, .ruff-cache, .uv-cache, .uv-cache-codex, .uv-tools, .uv-tools-codex -ErrorAction SilentlyContinue
Remove-Item -Force backend-dev.log -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force logs\* -ErrorAction SilentlyContinue
```

不要用该命令删除 `data/`、`.venv/`、数据库卷、用户办公文件或 Docker volume；它们不属于可再生的日志/缓存。

## 7. 排障速查

- 上传文档后"会话不存在"：确认 `data/office` 卷已挂载、`office_sessions` 表存在（`alembic upgrade head`）。
- 聊天 403"无权访问会话"：JWT 过期，重新登录。
- Beat 未跑：`docker compose ps` 检查 `lumi-beat`，缺失则 `docker compose up -d beat`。
- 嵌入模型加载失败：确认 `EMBEDDING_CACHE_DIR` 卷与模型缓存存在，或临时改 `EMBEDDING_DEVICE=cpu`。
