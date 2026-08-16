# Lumi Backend

多模态 AI 助手后端：聊天（流式 SSE）、RAG 知识库、长期记忆（加密）、办公模式（文档编辑/脚本执行/多智能体编排）、语音（TTS/ASR）。

## 技术栈

- Python 3.13 / FastAPI / SQLAlchemy(async) + PostgreSQL(pgvector)
- Redis（缓存 / Celery broker / 短期记忆 / 限流）
- Temporal（多智能体工作流编排，失败回退自建 DAG）
- Celery（异步任务 + Beat 定时调度）
- MCP 混合架构（服务端技能原生调用，客户端技能经 Electron MCP 直连）

## 快速启动（开发）

```bash
# 依赖（uv）
uv sync

# 配置
cp .env.example .env   # 填写密钥

# 数据库：先建库（PostgreSQL + pgvector），再迁移
uv run alembic upgrade head

# 启动后端
uv run uvicorn app.main:app --reload --port 8000
```

前端（Electron + Vite）在独立仓库，后端默认 `http://localhost:8000`。

## 常用命令

```bash
uv run pytest tests -q            # 测试
uv run alembic upgrade head       # 数据库迁移
uv run alembic revision --autogenerate -m "描述"   # 模型变更生成迁移
uv run celery -A celery_app.celery_app worker -l info   # 异步 worker
uv run celery -A celery_app.celery_app beat -l info     # 定时调度
```

## Docker 部署

```bash
docker compose up -d
# 生产建议叠加 Docker secrets：
# docker compose -f docker-compose.yml -f docker-compose.secrets.yml up -d
```

详见 [docs/OPERATIONS.md](docs/OPERATIONS.md)（部署/定时任务/迁移/密钥/备份/排障）与
[docs/DEGRADATION.md](docs/DEGRADATION.md)（降级与容错矩阵）。

## 架构文档

- [docs/RAG_DESIGN.md](docs/RAG_DESIGN.md) — 知识库检索
- [docs/MEMORY_DESIGN.md](docs/MEMORY_DESIGN.md) — 长期记忆与隐私
- [docs/OFFICE_SKILLS.md](docs/OFFICE_SKILLS.md) — 办公模式
- [docs/API_AUTH.md](docs/API_AUTH.md) / [docs/API_INTEGRATION.md](docs/API_INTEGRATION.md) — API 与鉴权
