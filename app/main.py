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

from app.agents.registry import init_agents
from app.api.router import api_router
from app.core.config import settings
from app.core.logging import setup_logging
from app.core.redis import close_redis, init_redis


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理."""
    setup_logging()
    logger.info(f"🚀 {settings.PROJECT_NAME} v{settings.VERSION} 启动中...")

    # 初始化基础设施
    init_agents()
    await init_redis()
    logger.info("基础设施初始化完成")

    yield

    # 清理
    await close_redis()
    logger.info("资源已清理")


def create_app() -> FastAPI:
    """创建 FastAPI 应用实例."""
    app = FastAPI(
        title=settings.PROJECT_NAME,
        version=settings.VERSION,
        description="Lumi - 桌面AI陪伴助手后端服务 | 多场景陪伴 · 知识库RAG · 长期记忆 · 多用户体系 · PC本地加速协同",
        lifespan=lifespan,
    )
    app.include_router(api_router, prefix="/api/v1")

    # CORS 跨域配置
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return app


app = create_app()

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="localhost", port=8000, reload=True)
