"""用户显式管理的外部 MCP 工具绑定。"""

from fastapi import APIRouter, Depends

from app.core.deps import require_auth
from app.core.exceptions import BadRequestException, NotFoundException
from app.models.mcp import McpToolBindingCreate, McpToolBindingUpdate
from app.services import mcp_bindings

router = APIRouter()


def _binding_view(binding) -> dict:
    return {
        "id": str(binding.id), "server_name": binding.server_name,
        "raw_tool_name": binding.raw_tool_name, "display_name": binding.display_name,
        "description": binding.description, "domain": binding.domain,
        "intent_tags": binding.intent_tags, "scenes": binding.scenes,
        "permission": binding.permission, "write_op": binding.write_op,
        "requires_confirmation": binding.requires_confirmation,
        "confirmation_mode": binding.confirmation_mode, "idempotent": binding.idempotent,
        "daily_call_limit": binding.daily_call_limit, "concurrency_limit": binding.concurrency_limit,
        "status": binding.status, "created_at": binding.created_at,
    }


@router.get("/servers/{server_name}/tools")
async def discover_tools(server_name: str, payload: dict = Depends(require_auth)):
    tools = await mcp_bindings.discover_bindable_tools(server_name)
    if not tools:
        raise NotFoundException("MCP Server 不存在、未获准绑定或当前不可用")
    return {"code": 0, "data": {"items": tools}}


@router.get("/bindings")
async def list_user_bindings(payload: dict = Depends(require_auth)):
    items = await mcp_bindings.list_bindings(payload["sub"])
    return {"code": 0, "data": {"items": [_binding_view(item) for item in items]}}


@router.post("/bindings")
async def bind_tool(req: McpToolBindingCreate, payload: dict = Depends(require_auth)):
    try:
        binding = await mcp_bindings.create_binding(payload["sub"], req)
    except ValueError as exc:
        raise BadRequestException(str(exc)) from exc
    return {"code": 0, "data": _binding_view(binding), "message": "MCP 工具已绑定"}


@router.post("/bindings/{binding_id}")
async def update_binding(binding_id: str, req: McpToolBindingUpdate, payload: dict = Depends(require_auth)):
    if not await mcp_bindings.set_binding_enabled(payload["sub"], binding_id, req.enabled):
        raise NotFoundException("MCP 工具绑定不存在")
    return {"code": 0, "message": "MCP 工具绑定已更新"}


@router.delete("/bindings/{binding_id}")
async def remove_binding(binding_id: str, payload: dict = Depends(require_auth)):
    if not await mcp_bindings.delete_binding(payload["sub"], binding_id):
        raise NotFoundException("MCP 工具绑定不存在")
    return {"code": 0, "message": "MCP 工具绑定已移除"}
