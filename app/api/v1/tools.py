"""客户端工具 API —— 用户端轮询待执行请求 + 回传结果."""

from fastapi import APIRouter, Depends

from app.core.deps import require_auth
from app.core.exceptions import BadRequestException
from app.models.client_tool import ClientToolResultRequest
from app.services import client_tools

router = APIRouter()


@router.get("/requests")
async def list_tool_requests(payload: dict = Depends(require_auth)):
    """用户端轮询：获取该用户的待执行工具请求."""
    items = await client_tools.list_pending_requests(payload["sub"])
    return {"code": 0, "data": {"items": items}}


@router.post("/requests/{request_id}/result")
async def submit_tool_result(
    request_id: str,
    req: ClientToolResultRequest,
    payload: dict = Depends(require_auth),
):
    """用户端回传执行结果."""
    ok = await client_tools.complete_request(
        payload["sub"],
        request_id,
        success=req.success,
        output=req.output,
        error=req.error,
        metadata=req.metadata,
    )
    if not ok:
        raise BadRequestException("请求不存在或已处理")
    return {"code": 0, "message": "已提交"}


@router.post("/requests/{request_id}/cancel")
async def cancel_tool_request(request_id: str, payload: dict = Depends(require_auth)):
    """用户端取消（确认弹窗点了取消）."""
    ok = await client_tools.cancel_request(payload["sub"], request_id)
    if not ok:
        raise BadRequestException("请求不存在或已处理")
    return {"code": 0, "message": "已取消"}
