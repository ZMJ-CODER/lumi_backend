"""FastAPI 依赖注入：JWT 鉴权、获取当前用户等."""

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.exceptions import ForbiddenException, UnauthorizedException
from app.platform.security.security import decode_token

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
    """强制要求登录，否则 401.

    401 必须带**机器可读**的 ``data.error_code``：前端据此决定"先用 refresh_token
    续期"还是"清掉本地令牌并弹登录框"。只给一句"请先登录"，客户端就只能字符串匹配，
    很容易把"令牌过期"当成"没登录"而静默降级成游客态——表现就是"页面什么都没有"
    （见 `docs/ARCHITECTURE_BOUNDARIES.md` §8.14.12 的现场排障）。
    """
    if not payload:
        raise UnauthorizedException(
            "请先登录",
            error_code="LOGIN_REQUIRED",
            data={"error_code": "LOGIN_REQUIRED", "action": "refresh_or_relogin"},
        )
    return payload


def require_admin(payload: dict = Depends(require_auth)) -> dict:
    """要求管理员角色."""
    if payload.get("role") not in ("admin", "superadmin"):
        raise ForbiddenException("需要管理员权限")
    return payload


def require_superadmin(payload: dict = Depends(require_auth)) -> dict:
    """要求超级管理员角色.

    这是管理面**唯一**的授权口径：管理员登录一次（superadmin JWT）即可读写运维面板。
    历史上还有一层"``X-Admin-Token`` 二次密码验证"（管理员密码换 5 分钟 token），
    已于 2026-09 移除；请求里若仍带该 header 会被忽略（不影响任何端点）。
    """
    if payload.get("role") != "superadmin":
        raise ForbiddenException("需要超级管理员权限")
    return payload
