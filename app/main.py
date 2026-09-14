"""Lumi 后端多智能体协作服务入口。

架构分层:
  api/           → FastAPI 路由层 (接口定义、参数校验)
  services/      → 业务逻辑层 (编排、场景管理、记忆提取)
  agents/        → 智能体层 (各场景 AI 人格实现) + capabilities/ 资源能力层
  workspace/ knowledge/ memory/ office/ → 四个业务域
  platform/      → 平台设施 (模型、安全、运行时、网络)
  observability/ → 日志与指标
  core/          → 配置、数据库、DI、异常映射
  models/        → 数据模型层 (Pydantic schema + SQLAlchemy ORM)
  celery_app/    → 异步任务层 (文档处理等耗时操作)

启动方式（推荐 ``uvicorn app.main:app``）：见 README。**也支持** ``python app/main.py``——
入口会先把 ``app/`` 从 ``sys.path`` 摘掉（见 :func:`ensure_import_root`）。
"""

import sys
from pathlib import Path


def ensure_import_root() -> None:
    """把仓库根放进 ``sys.path``，并把 ``app/`` 目录**移出去**。

    以脚本方式运行本文件（``python app/main.py``）时，Python 会把脚本所在目录
    ``app/`` 放到 ``sys.path[0]``。于是第三方库里的 ``import platform`` 会命中
    ``app/platform/``（同名包），一进 sqlalchemy 就炸：

    ```
    AttributeError: module 'platform' has no attribute 'python_implementation'
    ```

    （``platform`` 是 ``app/`` 下唯一与标准库重名的顶层名字。）这里把 ``app/`` 摘掉、
    把仓库根补到最前：``app.*`` 一律按包导入，标准库不会再被遮蔽。
    以模块方式启动（``uvicorn app.main:app`` / ``python -m app.main``）时本函数是空操作。

    幂等、可重复调用：每次按当前 ``sys.path`` 重新收敛，并把仓库根放到**最前**
    （本地源码优先于任何已安装副本）。
    """
    app_dir = Path(__file__).resolve().parent
    root = str(app_dir.parent)
    kept: list[str] = []
    for entry in sys.path:
        try:
            # 空串表示当前工作目录；解析后与 app 目录相同也要摘掉（否则照样遮蔽标准库）。
            if entry and Path(entry).resolve() == app_dir:
                continue
        except OSError:  # pragma: no cover - 无法解析的路径原样保留
            pass
        if entry == root:  # 仓库根稍后统一插到最前
            continue
        kept.append(entry)
    sys.path[:] = [root, *kept]


ensure_import_root()

from contextlib import asynccontextmanager  # noqa: E402

import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from loguru import logger  # noqa: E402

from app.agents.registry import init_agents  # noqa: E402
from app.api.router import api_router  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.exception_handlers import register_exception_handlers  # noqa: E402
from app.observability.logging import setup_logging  # noqa: E402
from app.observability.observability import (  # noqa: E402
    init_sentry,
    metrics_middleware,
    metrics_text,
    refresh_async_dispatch_metrics,
)
from app.core.redis import close_redis, init_redis  # noqa: E402
from app.platform.security.security_hardening import rate_limit_middleware, security_headers_middleware  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理."""
    setup_logging()
    from app.observability.logging import setup_uvicorn_queue_logging

    setup_uvicorn_queue_logging()
    init_sentry()
    logger.info(f" {settings.PROJECT_NAME} v{settings.VERSION} 启动中...")
    if settings.JWT_SECRET_KEY == "change-me-in-production":
        logger.warning("JWT_SECRET_KEY 仍为默认值！请在 .env 中配置随机密钥，否则令牌可被伪造")
    # 密钥指纹（不是密钥本身）：全站突然 401 时，用它和"令牌签发时的指纹"对比，
    # 一眼分清是密钥轮换、令牌过期，还是真的没登录（见 decode_token 的告警）。
    from app.platform.security.security import jwt_secret_fingerprint

    logger.info("JWT 密钥指纹={}（轮换会使所有已发出的令牌失效，客户端需重新登录）", jwt_secret_fingerprint())
    # .env 里同名键写两次时，只有**最后一条**生效：这会让"改了密钥却没生效/悄悄生效"
    # 变成难查的事故（实测：全站 401）。启动时点名提醒，不打印任何值。
    from app.core.config import duplicate_env_keys

    duplicates = duplicate_env_keys()
    if duplicates:
        logger.warning(
            ".env 存在重复键（只有最后一条生效，请合并为一条）：{}",
            ", ".join(duplicates),
        )

    # 初始化基础设施
    init_agents()
    from app.agents.skills.registry import init_skills

    init_skills()
    await init_redis()
    # Semantic Skill routing must be ready before normal traffic, or every
    # first request is explicitly tracked as lexical_fallback. This is not a
    # request-path warmup: lifecycle startup owns the (small) descriptor index.
    if settings.SKILL_SEMANTIC_ROUTING_ENABLED and settings.SKILL_SEMANTIC_ROUTING_STARTUP_WARMUP:
        try:
            from app.agents.skills.routing import warm_registered_skill_semantic_index

            ready = await warm_registered_skill_semantic_index()
            if not ready:
                logger.warning("Skill 语义路由未就绪，当前将记录 lexical_fallback；请检查嵌入模型配置")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skill 语义路由启动预热失败，当前将记录 lexical_fallback: {}", exc)
    # 策略与 Skill 生命周期独立。错误绝不阻断启动：引擎保留内置安全兜底。
    try:
        from app.agents.orchestration.planning.strategy_engine import strategy_engine

        await strategy_engine.reload()
    except Exception as exc:  # noqa: BLE001
        logger.warning("策略引擎预热失败，将使用内置安全兜底: {}", exc)
    # Schema ownership belongs exclusively to Alembic.  Deployment runs
    # ``alembic upgrade head`` before starting an API replica, so application
    # workers never race over DDL or silently patch production tables.
    # Reconcile only stale two-phase effect reservations. A fresh reservation
    # may belong to another healthy worker, hence the conservative grace period.
    try:
        from app.agents.orchestration.runtime.effects import recover_orphaned_effect_intents

        count = await recover_orphaned_effect_intents(
            settings.AGENT_EFFECT_INTENT_RECOVERY_GRACE_SECONDS
        )
        if count:
            logger.warning("已将 {} 条遗留副作用 intent 标记为不确定，禁止自动重试", count)
    except Exception as exc:  # noqa: BLE001
        # Startup remains available during a transient DB outage; every write
        # node still fails closed until journal access returns.
        logger.warning("副作用日志恢复扫描跳过: {}", exc)
    # 遥测只是候选排序的辅助信号，启动时预热缓存，绝不让用户工具选择首轮查库。
    try:
        from app.services.skill_telemetry import refresh_success_rate_hints

        await refresh_success_rate_hints("office")
        await refresh_success_rate_hints("chat")
    except Exception as exc:  # noqa: BLE001
        logger.debug("Skill 遥测缓存预热跳过: {}", exc)
    # 多智能体编排：Temporal Worker 随后端进程启动（未开则需独立进程跑 worker）
    if (
        settings.AGENT_ORCHESTRATION == "temporal"
        and settings.TEMPORAL_RUN_WORKER_INPROCESS
    ):
        from app.agents.orchestration.temporal.runtime import start_inprocess_worker

        await start_inprocess_worker()
    # 启动时兜底清理一次后端生成的临时/产物文件（定时任务由 Celery beat 负责）：
    # 用户产物与归档按各自的保留类别清理，互不越界。
    try:
        from app.services.artifact_retention import cleanup_archive_outputs
        from app.office.docs import cleanup_expired_sessions, cleanup_generic_outputs

        await cleanup_expired_sessions()
        cleanup_generic_outputs(settings.GENERATED_FILES_TTL_DAYS)
        cleanup_archive_outputs()
    except Exception as exc:  # noqa: BLE001 - 清理失败不阻塞启动
        logger.warning("启动清理生成文件失败（忽略）: {}", exc)
    logger.info("基础设施初始化完成")

    # 能力撤销广播订阅：跨 worker 撤销要立刻失效本地租约缓存，否则撤销会有窗口期
    # （客户端已隔离但别的 worker 仍按旧缓存派发）。Redis 不可用时静默跳过。
    try:
        from app.services.capability_revoke import start_revoke_listener

        subscribed = await start_revoke_listener()
        if subscribed:
            logger.info("能力撤销广播已订阅（跨 worker 缓存失效就绪）")
    except Exception as exc:  # noqa: BLE001 - 订阅失败不阻断启动
        logger.warning("能力撤销订阅失败（忽略）: {}", str(exc)[:160])

    # 运行时策略轮询（方案 §1）：每 POLICY_POLL_INTERVAL_SECONDS 查一次 policy:epoch，
    # 变了才全量拉取。开关关闭时 start_polling 直接返回 None（零开销、不起协程）。
    try:
        from app.services.runtime_policy import policy_store

        task = await policy_store.start_polling()
        if task is not None:
            logger.info("运行时策略轮询已启动（间隔 {}s）", policy_store.snapshot()["poll_interval_seconds"])
    except Exception as exc:  # noqa: BLE001 - 轮询失败不能阻断启动（沿用代码默认值）
        logger.warning("运行时策略轮询启动失败（忽略）: {}", str(exc)[:160])

    yield

    # 清理
    if (
        settings.AGENT_ORCHESTRATION == "temporal"
        and settings.TEMPORAL_RUN_WORKER_INPROCESS
    ):
        from app.agents.orchestration.temporal.runtime import stop_inprocess_worker

        await stop_inprocess_worker()
    # 关闭 MCP 客户端会话（客户端技能直连 Electron MCP server）
    try:
        from app.agents.mcp.manager import close_all

        await close_all()
    except Exception:  # noqa: BLE001
        pass
    # 停止能力撤销订阅（避免退出时留下挂起的后台任务）
    try:
        from app.services.capability_revoke import stop_revoke_listener

        await stop_revoke_listener()
    except Exception:  # noqa: BLE001
        pass
    # 停止运行时策略轮询（同样不留挂起的后台任务）
    try:
        from app.services.runtime_policy import policy_store

        await policy_store.aclose()
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
