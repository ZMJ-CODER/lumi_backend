"""Lumi Backend - 多智能体协作后端服务入口.

架构分层:
  api/       → FastAPI 路由层 (接口定义、参数校验)
  services/  → 业务逻辑层 (编排、场景管理、记忆提取)
  agents/    → 智能体层 (各场景 AI 人格实现)
  core/      → 基础设施层 (配置、DB、Redis、LLM、安全)
  models/    → 数据模型层 (Pydantic schema + SQLAlchemy ORM)
  celery_app/ → 异步任务层 (文档处理等耗时操作)
"""

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pathlib import Path

from app.agents.registry import init_agents
from app.api.router import api_router
from app.core.config import settings
from app.core.exception_handlers import register_exception_handlers
from app.core.logging import setup_logging
from app.core.observability import (
    init_sentry,
    metrics_middleware,
    metrics_text,
    refresh_async_dispatch_metrics,
)
from app.core.redis import close_redis, init_redis
from app.core.security_hardening import rate_limit_middleware, security_headers_middleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理."""
    setup_logging()
    from app.core.logging import setup_uvicorn_queue_logging

    setup_uvicorn_queue_logging()
    init_sentry()
    logger.info(f" {settings.PROJECT_NAME} v{settings.VERSION} 启动中...")
    if settings.JWT_SECRET_KEY == "change-me-in-production":
        logger.warning("JWT_SECRET_KEY 仍为默认值！请在 .env 中配置随机密钥，否则令牌可被伪造")

    # 初始化基础设施
    init_agents()
    from app.agents.skills.registry import init_skills

    init_skills()
    await init_redis()
    # 自动补齐缺失的数据表（幂等：仅创建不存在的表；新增表如 user_preferences 无需手动迁移）
    try:
        from app.core.database import engine
        from app.models.db_models import Base

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:  # noqa: BLE001 - 建表失败不阻塞启动（可能无 DB 权限）
        logger.warning("自动建表跳过（可运行 scripts/init_db.py 手动补齐）: {}", exc)
    # 增量字段补齐（幂等）：create_all 只建新表，老表新增列需显式 ALTER
    try:
        from sqlalchemy import text as _sql_text
        from app.core.database import engine as _engine

        async with _engine.begin() as _conn:
            await _conn.execute(
                _sql_text(
                    "ALTER TABLE user_preferences "
                    "ADD COLUMN IF NOT EXISTS email_client VARCHAR(32) NOT NULL DEFAULT ''"
                )
            )
    except Exception as exc:  # noqa: BLE001 - 列已存在/无权限时静默
        logger.debug("增量字段补齐跳过: {}", exc)
    # 遥测只是候选排序的辅助信号，启动时预热缓存，绝不让用户工具选择首轮查库。
    try:
        from app.services.skill_telemetry import refresh_success_rate_hints

        await refresh_success_rate_hints("office")
        await refresh_success_rate_hints("chat")
    except Exception as exc:  # noqa: BLE001
        logger.debug("Skill 遥测缓存预热跳过: {}", exc)
    # 多智能体编排：Temporal Worker 随后端进程启动（未开则需独立进程跑 worker）
    if settings.AGENT_ORCHESTRATION == "manifest_temporal" and settings.TEMPORAL_RUN_WORKER_INPROCESS:
        from app.agents.orchestration.temporal.runtime import start_inprocess_worker

        await start_inprocess_worker()
    # 启动时兜底清理一次后端生成的临时/产物文件（定时任务由 Celery beat 负责）
    try:
        from app.services.office_docs import cleanup_expired_sessions, cleanup_generic_outputs

        await cleanup_expired_sessions()
        cleanup_generic_outputs(settings.GENERATED_FILES_TTL_DAYS)
    except Exception as exc:  # noqa: BLE001 - 清理失败不阻塞启动
        logger.warning("启动清理生成文件失败（忽略）: {}", exc)
    logger.info("基础设施初始化完成")

    yield

    # 清理
    if settings.TEMPORAL_RUN_WORKER_INPROCESS:
        from app.agents.orchestration.temporal.runtime import stop_inprocess_worker

        await stop_inprocess_worker()
    # 关闭 MCP 客户端会话（客户端技能直连 Electron MCP server）
    try:
        from app.agents.mcp.manager import close_all

        await close_all()
    except Exception:  # noqa: BLE001
        pass
    await close_redis()
    logger.info("资源已清理")


def create_app() -> FastAPI:
    """创建 FastAPI 应用实例."""
    app = FastAPI(
        title=settings.PROJECT_NAME,
        version=settings.VERSION,
        description="Lumi",
        lifespan=lifespan,
    )

    # 注册全局统一异常处理器（所有未捕获/业务异常统一输出规范格式并完整记录日志）
    register_exception_handlers(app)
    app.middleware("http")(metrics_middleware)
    app.middleware("http")(rate_limit_middleware)
    app.middleware("http")(security_headers_middleware)

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        """Prometheus 指标（可观测性；配合 Grafana 告警）."""
        from fastapi.responses import PlainTextResponse

        await refresh_async_dispatch_metrics()
        return PlainTextResponse(metrics_text())

    app.include_router(api_router, prefix="/api/v1")

    # 聊天附件目录（访问走签名 URL 接口，不再静态裸挂）
    chat_upload_dir = Path(settings.UPLOAD_DIR) / "chat"
    chat_upload_dir.mkdir(parents=True, exist_ok=True)

    # CORS 跨域配置
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        # 通配符不允许带凭据（规范限制）；配置了具体域名才允许凭据
        allow_credentials="*" not in settings.CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return app


app = create_app()

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="localhost",
        port=8000,
        reload=True,
        access_log=False,  # 关闭 uvicorn 默认的每请求访问日志，减少刷屏
    )
