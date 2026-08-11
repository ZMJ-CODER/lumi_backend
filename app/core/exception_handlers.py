"""统一全局异常处理器 —— 兜底所有异常并输出统一响应格式.

响应格式（与 docs/API_INTEGRATION.md 约定一致）:
  {"code": <业务码>, "message": "提示", "data": null}

覆盖范围:
  1. AppException（含全部子类）—— 业务错误，按各自 status_code 返回
  2. RequestValidationError —— FastAPI 参数校验失败 → 400
  3. StarletteHTTPException —— 路由未匹配、405 等 FastAPI/Starlette 原生异常
  4. Exception —— 未知异常兜底 → 500，并记录完整堆栈
"""

import traceback

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import AppException


def register_exception_handlers(app: FastAPI) -> None:
    """注册全部统一异常处理器（在 app/main.py 的 create_app 中调用）."""
    app.add_exception_handler(AppException, _app_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)


def _build_error_response(status_code: int, code: int, message: str, data: object | None = None) -> JSONResponse:
    """构造统一错误响应体."""
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "data": data,
        },
    )


async def _app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    """AppException 及其子类：业务错误，按自身状态码返回."""
    # 5xx 视为服务端错误，记录 error；4xx 仅记录 warning（避免刷屏）
    # 5xx 记录 error；4xx 仅 DEBUG（避免 401/403 等高频业务错误刷屏）
    log = logger.error if exc.status_code >= 500 else logger.debug
    log(
        "[AppException] {} {} | code={} | message={} | error_code={}",
        request.method, request.url.path, exc.code, exc.message, exc.error_code,
    )
    return _build_error_response(exc.status_code, exc.code, exc.message, exc.data)


async def _validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI 参数/请求体校验失败 → 统一 400."""
    errors = exc.errors()
    first = errors[0] if errors else {}
    # 提取首个错误的位置与原因，便于前端/日志定位
    loc = ".".join(str(x) for x in first.get("loc", [])) if first.get("loc") else ""
    msg = first.get("msg", "请求参数错误")
    detail = f"{loc}: {msg}" if loc else msg
    logger.warning("[参数校验失败] {} {} | detail={}", request.method, request.url.path, detail)
    return _build_error_response(400, 400, f"请求参数错误: {detail}")


async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """兼容 FastAPI/Starlette 原生 HTTPException（存量代码或第三方组件直接抛出）."""
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    log = logger.error if exc.status_code >= 500 else logger.debug
    log("[HTTPException] {} {} | status={} | detail={}", request.method, request.url.path, exc.status_code, detail)
    return _build_error_response(exc.status_code, exc.status_code, detail)


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """未知异常兜底：记录完整堆栈，返回统一 500."""
    traceback_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    logger.error(
        "[未捕获异常] {} {}\n{}",
        request.method, request.url.path, traceback_text,
    )
    return _build_error_response(500, 500, "服务器内部错误")
