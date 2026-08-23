"""可观测性：Sentry 错误上报 + Prometheus 指标.

- Sentry：配置 SENTRY_DSN 后自动捕获未处理异常（FastAPI 集成）；
- Prometheus：/metrics 暴露 HTTP 请求量/延迟、任务结果、技能调用等指标，
  供 Prometheus + Grafana 采集。
"""

from __future__ import annotations

import time
from typing import Callable

from loguru import logger

from app.core.config import settings

# ── Sentry ──


def init_sentry() -> None:
    """初始化 Sentry（未配置 DSN 时跳过）."""
    if not settings.SENTRY_DSN:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            environment="production" if not settings.DEBUG else "development",
            traces_sample_rate=0.1,
            integrations=[
                StarletteIntegration(transaction_style="endpoint"),
                FastApiIntegration(transaction_style="endpoint"),
            ],
            send_default_pii=False,
        )
        logger.info("Sentry 已启用")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Sentry 初始化失败: {}", exc)


# ── Prometheus 指标 ──

_prometheus = None
_http_requests = None
_http_duration = None
_agent_jobs = None
_skill_calls = None
_skill_routing_modes = None
_rag_searches = None
_agent_routes = None
_agent_replans = None
_plan_cache = None
_agent_route_duration = None
_agent_node_duration = None
_agent_channel_wait = None
_manifest_route_upgrades = None
_celery_queue_ready = None
_document_pipeline_state = None
_document_pipeline_oldest_age = None


def _ensure_metrics():
    """懒加载 prometheus-client 指标（避免未安装/未启用时阻塞启动）."""
    global _prometheus, _http_requests, _http_duration, _agent_jobs, _skill_calls, _skill_routing_modes, _rag_searches
    global _agent_routes, _agent_replans, _plan_cache, _agent_route_duration, _agent_node_duration
    global _agent_channel_wait, _manifest_route_upgrades
    global _celery_queue_ready, _document_pipeline_state, _document_pipeline_oldest_age
    if _prometheus is not None:
        return True
    if not settings.METRICS_ENABLED:
        return False
    try:
        from prometheus_client import Counter, Gauge, Histogram

        _http_requests = Counter(
            "lumi_http_requests_total", "HTTP 请求总数", ["method", "path", "status"]
        )
        _http_duration = Histogram(
            "lumi_http_request_duration_seconds",
            "HTTP 请求耗时（秒）",
            ["method", "path"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
        )
        _agent_jobs = Counter("lumi_agent_jobs_total", "多智能体任务结果", ["status"])
        _skill_calls = Counter("lumi_skill_calls_total", "技能调用次数", ["skill", "success"])
        _skill_routing_modes = Counter(
            "lumi_skill_routing_modes_total",
            "Skill 候选路由模式；lexical_fallback 表示语义索引未就绪或不可用",
            ["scene", "mode"],
        )
        _rag_searches = Counter(
            "lumi_rag_searches_total", "RAG 检索次数", ["hits"]
        )
        _agent_routes = Counter(
            "lumi_agent_routes_total", "办公任务路由结果", ["level", "mode", "cache_hit"]
        )
        _agent_replans = Counter(
            "lumi_agent_replans_total", "办公任务升级或重规划", ["from_level", "to_level", "reason"]
        )
        _plan_cache = Counter(
            "lumi_agent_plan_cache_total", "办公计划缓存事件", ["result"]
        )
        _agent_route_duration = Histogram(
            "lumi_agent_route_duration_seconds",
            "办公任务评估与规划耗时",
            ["level", "mode"],
            buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5, 15, 30, 60),
        )
        _agent_node_duration = Histogram(
            "lumi_agent_node_duration_seconds",
            "办公原子节点执行耗时",
            ["agent", "success"],
            buckets=(0.01, 0.05, 0.1, 0.5, 1, 2.5, 5, 15, 30, 60, 180, 300),
        )
        _agent_channel_wait = Histogram(
            "lumi_agent_channel_wait_seconds",
            "办公四层路由通道限流等待时间",
            ["channel"],
            buckets=(0.001, 0.01, 0.05, 0.1, 0.5, 1, 5, 15, 30, 60, 300),
        )
        _manifest_route_upgrades = Counter(
            "lumi_manifest_route_upgrades_total",
            "任务清单原子项通道升级次数",
            ["from_channel", "to_channel", "reason"],
        )
        _celery_queue_ready = Gauge(
            "lumi_celery_queue_ready_tasks",
            "Celery Redis broker ready-task depth (does not include in-flight tasks)",
            ["queue"],
        )
        _document_pipeline_state = Gauge(
            "lumi_document_pipeline_documents",
            "Knowledge documents grouped by durable processing state",
            ["status"],
        )
        _document_pipeline_oldest_age = Gauge(
            "lumi_document_pipeline_oldest_age_seconds",
            "Age of the oldest queued or processing document",
            ["status"],
        )
        _prometheus = True
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Prometheus 指标初始化失败: {}", exc)
        _prometheus = False
        return False


def inc_http_request(method: str, path: str, status: int, duration: float) -> None:
    if not _ensure_metrics():
        return
    route = path.split("?")[0][:120]
    _http_requests.labels(method=method, path=route, status=str(status)).inc()
    _http_duration.labels(method=method, path=route).observe(duration)


def inc_agent_job(status: str) -> None:
    if _ensure_metrics():
        _agent_jobs.labels(status=status).inc()


def inc_skill_call(skill: str, success: bool) -> None:
    if _ensure_metrics():
        _skill_calls.labels(skill=skill or "unknown", success=str(bool(success))).inc()


def inc_skill_routing_mode(scene: str, mode: str) -> None:
    if _ensure_metrics():
        _skill_routing_modes.labels(
            scene=(scene or "unknown")[:40],
            mode=(mode or "unknown")[:40],
        ).inc()


def inc_rag_search(hits: int) -> None:
    if _ensure_metrics():
        _rag_searches.labels(hits="hit" if hits else "miss").inc()


def inc_agent_route(level: str, mode: str, cache_hit: bool, duration: float | None = None) -> None:
    if _ensure_metrics():
        _agent_routes.labels(
            level=level or "unknown",
            mode=mode or "unknown",
            cache_hit=str(bool(cache_hit)).lower(),
        ).inc()
        if duration is not None:
            _agent_route_duration.labels(
                level=level or "unknown", mode=mode or "unknown"
            ).observe(max(0.0, duration))


def inc_agent_replan(from_level: str, to_level: str, reason: str) -> None:
    if _ensure_metrics():
        _agent_replans.labels(
            from_level=from_level or "unknown",
            to_level=to_level or "unknown",
            reason=reason or "unknown",
        ).inc()


def inc_plan_cache(result: str) -> None:
    if _ensure_metrics():
        _plan_cache.labels(result=result or "unknown").inc()


def observe_agent_node_duration(agent: str, success: bool, duration: float) -> None:
    if _ensure_metrics():
        _agent_node_duration.labels(
            agent=(agent or "unknown")[:80], success=str(bool(success)).lower()
        ).observe(max(0.0, duration))


def observe_agent_channel_wait(channel: str, duration: float) -> None:
    if _ensure_metrics():
        _agent_channel_wait.labels(channel=(channel or "unknown")[:80]).observe(max(0.0, duration))


def inc_manifest_route_upgrade(from_channel: str, to_channel: str, reason: str) -> None:
    if _ensure_metrics():
        _manifest_route_upgrades.labels(
            from_channel=(from_channel or "unknown")[:80],
            to_channel=(to_channel or "unknown")[:80],
            reason=(reason or "unknown")[:80],
        ).inc()


async def refresh_async_dispatch_metrics() -> None:
    """Refresh cross-process Celery/document gauges just before /metrics.

    Redis LLEN represents only ready messages.  The document-state gauges and
    oldest-age gauges make in-flight and worker-lost work visible alongside it.
    Failures are intentionally isolated: observability must never break the
    scrape endpoint or the API request path.
    """
    if not _ensure_metrics():
        return
    try:
        import redis.asyncio as aioredis
        from sqlalchemy import func, select

        from app.core.database import async_session_factory
        from app.models.db_models import Document

        broker = aioredis.from_url(settings.CELERY_BROKER_URL, decode_responses=True)
        try:
            for queue in ("durable", "best_effort", "maintenance"):
                _celery_queue_ready.labels(queue=queue).set(await broker.llen(queue))
        finally:
            await broker.aclose()

        now = time.time()
        async with async_session_factory() as session:
            for status in ("pending", "processing", "ready", "error"):
                count = (
                    await session.execute(
                        select(func.count()).select_from(Document).where(Document.status == status)
                    )
                ).scalar_one()
                _document_pipeline_state.labels(status=status).set(count)

            for status, time_column in (
                ("pending", Document.queued_at),
                ("processing", Document.processing_started_at),
            ):
                oldest = (
                    await session.execute(
                        select(func.min(time_column)).where(Document.status == status)
                    )
                ).scalar_one()
                age = 0.0 if oldest is None else max(0.0, now - oldest.timestamp())
                _document_pipeline_oldest_age.labels(status=status).set(age)
    except Exception as exc:  # noqa: BLE001
        logger.debug("异步任务指标刷新失败: {}", exc)


def metrics_text() -> str:
    """生成 Prometheus 文本格式指标（/metrics 响应体）."""
    if not _ensure_metrics():
        return "# metrics disabled\n"
    from prometheus_client import generate_latest

    return generate_latest().decode("utf-8")


async def metrics_middleware(request, call_next: Callable):
    """FastAPI 中间件：记录请求量/耗时/状态."""
    method = request.method
    path = request.url.path
    start = time.perf_counter()
    try:
        response = await call_next(request)
        inc_http_request(method, path, response.status_code, time.perf_counter() - start)
        return response
    except Exception:
        inc_http_request(method, path, 500, time.perf_counter() - start)
        raise
