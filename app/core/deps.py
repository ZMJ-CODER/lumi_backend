"""FastAPI 依赖注入：JWT 鉴权、获取当前用户等."""

from fastapi import Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.exceptions import ForbiddenException, UnauthorizedException
from app.core.security import decode_token

security_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
) -> dict:
    """从 Authorization Header 解析 JWT，返回 payload.
    未登录返回空 dict，由具体接口自行决定是否要求登录.
    """
    if not credentials:
        return {}
    try:
        payload = decode_token(credentials.credentials)
        return payload
    except Exception:
        return {}


def require_auth(payload: dict = Depends(get_current_user)) -> dict:
    """强制要求登录，否则 401."""
    if not payload:
        raise UnauthorizedException("请先登录")
    return payload


def require_admin(payload: dict = Depends(require_auth)) -> dict:
    """要求管理员角色."""
    if payload.get("role") not in ("admin", "superadmin"):
        raise ForbiddenException("需要管理员权限")
    return payload


def require_superadmin(payload: dict = Depends(require_auth)) -> dict:
    """要求超级管理员角色."""
    if payload.get("role") != "superadmin":
        raise ForbiddenException("需要超级管理员权限")
    return payload


async def get_admin_verified_token(x_admin_token: str | None = Header(default=None)) -> str | None:
    """从 X-Admin-Token Header 获取管理员二次验证 token."""
    return x_admin_token