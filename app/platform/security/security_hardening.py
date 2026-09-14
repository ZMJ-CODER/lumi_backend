"""安全加固：Redis 全局限流 + 安全响应头（与 security.py 的 JWT/密码逻辑分开）."""

from __future__ import annotations

import time

from fastapi import Request

from app.core.config import settings


def _client_ip(request: Request) -> str:
    # 生产在反代后：从 X-Forwarded-For 取首个 IP（反代需保证该头可信）
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return str(fwd).split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def rate_limit_middleware(request: Request, call_next):
    """按 IP 限流：auth 路由更严，其余通用；Redis 不可用时放行（不因限流击穿）."""
    if settings.RATE_LIMIT_ENABLED:
        path = request.url.path
        # 健康检查/指标采集不应消耗用户的通用 IP 配额，否则高频探活会误伤业务。
        if path not in {"/metrics", "/api/v1/health", "/health"}:
            is_auth = path.startswith("/api/v1/auth")
            limit = (
                settings.RATE_LIMIT_AUTH_PER_MINUTE
                if is_auth
                else settings.RATE_LIMIT_GENERAL_PER_MINUTE
            )
            ip = _client_ip(request)
            window = int(time.time() // 60)
            key = f"rl:{ip}:{'auth' if is_auth else 'gen'}:{window}"
            try:
                from app.core.redis import get_redis

                r = get_redis()
                count = await r.incr(key)
                if count == 1:
                    await r.expire(key, 120)
                if count > limit:
                    return await _rate_limited_response(request)
            except Exception:  # noqa: BLE001
                pass  # Redis 不可用 → 放行
    response = await call_next(request)
    return response


async def _rate_limited_response(request: Request):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=429,
        content={"code": 429, "message": "请求过于频繁，请稍后再试", "data": None},
        headers={"Retry-After": "60"},
    )


async def security_headers_middleware(request: Request, call_next):
    """设置安全响应头（CSP 保守配置，避免破坏前端加载）."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; connect-src 'self' ws: wss:",
    )
    return response
