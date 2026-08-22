"""管理员审核外部 MCP 绑定。"""

from fastapi import APIRouter, Depends

from app.core.deps import require_superadmin
from app.core.exceptions import NotFoundException
from app.models.mcp import McpToolBindingReview
from app.services import mcp_bindings

router = APIRouter()


def _view(binding) -> dict:
    return {
        "id": str(binding.id), "user_id": str(binding.user_id),
        "server_name": binding.server_name, "raw_tool_name": binding.raw_tool_name,
        "description": binding.description, "domain": binding.domain,
        "intent_tags": binding.intent_tags, "write_op": binding.write_op,
        "requires_confirmation": binding.requires_confirmation,
        "daily_call_limit": binding.daily_call_limit,
        "concurrency_limit": binding.concurrency_limit,
        "status": binding.status, "created_at": binding.created_at,
    }


@router.get("/bindings/pending")
async def list_pending_bindings(payload: dict = Depends(require_superadmin)):
    items = await mcp_bindings.list_pending_bindings()
    return {"code": 0, "data": {"items": [_view(item) for item in items]}}


@router.post("/bindings/{binding_id}/review")
async def review_binding(
    binding_id: str,
    req: McpToolBindingReview,
    payload: dict = Depends(require_superadmin),
):
    binding = await mcp_bindings.review_binding(binding_id, req.approved)
    if binding is None:
        raise NotFoundException("待审核 MCP 工具绑定不存在或已处理")
    return {"code": 0, "data": _view(binding), "message": "MCP 工具绑定审核完成"}
