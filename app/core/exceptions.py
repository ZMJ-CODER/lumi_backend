"""统一自定义异常 —— 项目唯一异常类型来源.

设计目标:
  - 所有业务/系统异常统一继承 AppException，API 层不再直接抛 FastAPI HTTPException
  - 每种异常携带独立的业务 code（与 HTTP 状态码解耦，便于前端统一处理）
  - 保留原有全部错误文案，仅更换异常类型，不改变任何业务语义

约定:
  - 成功: code=0
  - 业务/HTTP 错误: code 与 HTTP 状态码保持一致（400/401/403/404/409/429/500）
  - 未捕获的未知异常由全局处理器兜底为 500
"""

from typing import Any


class AppException(Exception):
    """统一异常基类 —— 所有业务异常的父类.

    Attributes:
        code:       业务错误码（默认取 HTTP 状态码）
        status_code: HTTP 状态码
        message:    错误提示文案（原 detail 原样保留）
        data:       附加数据（可选，默认 None）
        error_code: 内部错误标识（可选，便于日志定位）
    """

    def __init__(
        self,
        status_code: int = 500,
        message: str = "服务器内部错误",
        code: int | None = None,
        data: Any = None,
        error_code: str = "",
    ) -> None:
        self.status_code = status_code
        self.code = code if code is not None else status_code
        self.message = message
        self.data = data
        self.error_code = error_code
        super().__init__(message)


# ── 客户端错误（4xx）──────────────────────────────────────

class BadRequestException(AppException):
    """400 参数错误 / 校验不通过."""

    def __init__(self, message: str = "请求参数错误", **kwargs: Any) -> None:
        super().__init__(status_code=400, message=message, **kwargs)


class UnauthorizedException(AppException):
    """401 未登录 / 令牌无效或过期."""

    def __init__(self, message: str = "请先登录", **kwargs: Any) -> None:
        super().__init__(status_code=401, message=message, **kwargs)


class ForbiddenException(AppException):
    """403 权限不足 / 账号禁用."""

    def __init__(self, message: str = "没有权限执行此操作", **kwargs: Any) -> None:
        super().__init__(status_code=403, message=message, **kwargs)


class NotFoundException(AppException):
    """404 资源不存在."""

    def __init__(self, message: str = "资源不存在", **kwargs: Any) -> None:
        super().__init__(status_code=404, message=message, **kwargs)


class ConflictException(AppException):
    """409 资源冲突（如账号已存在）."""

    def __init__(self, message: str = "资源冲突", **kwargs: Any) -> None:
        super().__init__(status_code=409, message=message, **kwargs)


class RateLimitException(AppException):
    """429 请求过于频繁 / 已被锁定."""

    def __init__(self, message: str = "请求过于频繁，请稍后再试", **kwargs: Any) -> None:
        super().__init__(status_code=429, message=message, **kwargs)


# ── 服务端错误（5xx）──────────────────────────────────────

class InternalServerException(AppException):
    """500 服务器内部错误（未知异常兜底）."""

    def __init__(self, message: str = "服务器内部错误", **kwargs: Any) -> None:
        super().__init__(status_code=500, message=message, **kwargs)


# ── 便捷函数 ──────────────────────────────────────────────

def raise_bad_request(message: str = "请求参数错误", **kwargs: Any) -> None:
    """抛 400 异常."""
    raise BadRequestException(message, **kwargs)


def raise_unauthorized(message: str = "请先登录", **kwargs: Any) -> None:
    """抛 401 异常."""
    raise UnauthorizedException(message, **kwargs)


def raise_forbidden(message: str = "没有权限执行此操作", **kwargs: Any) -> None:
    """抛 403 异常."""
    raise ForbiddenException(message, **kwargs)


def raise_not_found(message: str = "资源不存在", **kwargs: Any) -> None:
    """抛 404 异常."""
    raise NotFoundException(message, **kwargs)


def raise_conflict(message: str = "资源冲突", **kwargs: Any) -> None:
    """抛 409 异常."""
    raise ConflictException(message, **kwargs)


def raise_rate_limit(message: str = "请求过于频繁，请稍后再试", **kwargs: Any) -> None:
    """抛 429 异常."""
    raise RateLimitException(message, **kwargs)