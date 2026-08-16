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
_rag_searches = None


def _ensure_metrics():
    """懒加载 prometheus-client 指标（避免未安装/未启用时阻塞启动）."""
    global _prometheus, _http_requests, _http_duration, _agent_jobs, _skill_calls, _rag_searches
    if _prometheus is not None:
        return True
    if not settings.METRICS_ENABLED:
        return False
    try:
        from prometheus_client import Counter, Histogram

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
        _rag_searches = Counter(
            "lumi_rag_searches_total", "RAG 检索次数", ["hits"]
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


def inc_rag_search(hits: int) -> None:
    if _ensure_metrics():
        _rag_searches.labels(hits="hit" if hits else "miss").inc()


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
