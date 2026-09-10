"""工作区注册与绑定 API（纯元数据，不接收或保存工作区文件内容）。

工作区内容由 Electron 本地工作区持有并通过桌面 MCP 读写；本接口只负责：
  - 签发 / 列出 workspace_id；
  - conversation_id -> workspace_id 一对一绑定；
  - 工作区 ↔ 桌面设备（device_id / device_server）注册；
  - 按 user 归属校验工作区存在性。

不再提供文件上传、目录、tree、下载、snapshot/rollback/commit 等
服务端文件镜像端点（已停用，见 app/services/workspaces.py 模块说明）。
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.deps import require_auth
from app.core.exceptions import BadRequestException, NotFoundException
from app.services import workspaces

router = APIRouter()


class WorkspaceCreate(BaseModel):
    name: str = Field(default="未命名项目", max_length=200)
    conversation_id: str | None = Field(default=None, max_length=128)
    device_id: str | None = Field(default=None, max_length=80)
    device_server: str | None = Field(default=None, max_length=80)


class BindRequest(BaseModel):
    conversation_id: str = Field(..., min_length=1, max_length=128)


class DeviceRequest(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=80)
    device_server: str | None = Field(default=None, max_length=80)
    conversation_id: str | None = Field(default=None, max_length=128)


class RefreshRequest(BaseModel):
    force: bool = Field(default=False, description="是否忽略缓存强制重新读取目录")


@router.post("")
async def create(req: WorkspaceCreate, payload: dict = Depends(require_auth)):
    """创建（或复用一个已绑定会话的）工作区注册，不创建任何服务端文件目录。"""
    if req.conversation_id:
        existing = workspaces.workspace_for_conversation(payload["sub"], req.conversation_id)
        if existing:
            return {"code": 0, "data": existing}
    try:
        return {"code": 0, "data": workspaces.create_workspace(
            payload["sub"], req.name, req.conversation_id,
            device_id=req.device_id, device_server=req.device_server,
        )}
    except LookupError as exc:
        raise NotFoundException(str(exc)) from exc
    except ValueError as exc:
        raise BadRequestException(str(exc), error_code="WORKSPACE_BINDING_INVALID") from exc


@router.get("")
async def list_workspaces(payload: dict = Depends(require_auth)):
    """列出当前用户拥有的工作区注册元数据。"""
    return {"code": 0, "data": {"items": workspaces.list_workspaces(payload["sub"])}}


@router.get("/{workspace_id}")
async def get_workspace(workspace_id: str, payload: dict = Depends(require_auth)):
    """读取单个工作区元数据（同时校验归属与存在性）。"""
    try:
        return {"code": 0, "data": workspaces.get_workspace(payload["sub"], workspace_id)}
    except LookupError as exc:
        raise NotFoundException(str(exc)) from exc


@router.post("/{workspace_id}/device")
async def register_device(
    workspace_id: str,
    req: DeviceRequest,
    payload: dict = Depends(require_auth),
):
    """记录托管该工作区的桌面设备与 MCP server（仅元数据，不保存文件内容）。"""
    try:
        return {"code": 0, "data": workspaces.register_workspace_device(
            payload["sub"], workspace_id,
            device_id=req.device_id,
            device_server=req.device_server,
            conversation_id=req.conversation_id,
        )}
    except LookupError as exc:
        raise NotFoundException(str(exc)) from exc
    except ValueError as exc:
        raise BadRequestException(str(exc), error_code="WORKSPACE_BINDING_INVALID") from exc


@router.post("/{workspace_id}/bind")
async def bind_workspace(
    workspace_id: str,
    req: BindRequest,
    payload: dict = Depends(require_auth),
):
    """把该工作区绑定到一个会话（一对一，重复绑定同一会话为幂等）。"""
    try:
        return {"code": 0, "data": workspaces.bind_workspace_to_conversation(
            payload["sub"], workspace_id, req.conversation_id,
        )}
    except LookupError as exc:
        raise NotFoundException(str(exc)) from exc
    except ValueError as exc:
        raise BadRequestException(str(exc), error_code="WORKSPACE_BINDING_INVALID") from exc


@router.post("/{workspace_id}/refresh")
async def refresh_workspace_context(
    workspace_id: str,
    req: RefreshRequest,
    payload: dict = Depends(require_auth),
):
    """Electron 版本变化通知：失效并按需重建工作区上下文缓存。

    上传/删除/提交/回滚等文件变化后调用；``force=true`` 会立即重读目录，
    ``false`` 仅作失效，下一轮由版本探测自然重建。
    """
    try:
        workspaces.ensure_workspace(payload["sub"], workspace_id)
    except LookupError as exc:
        raise NotFoundException(str(exc)) from exc
    from app.services.workspace_context import invalidate_workspace_context

    await invalidate_workspace_context(workspace_id)
    summary_refreshed = False
    if req.force:
        try:
            from app.services.workspace_context import load_workspace_context

            ctx = await load_workspace_context(
                payload["sub"], workspace_id=workspace_id, force_refresh=True
            )
            summary_refreshed = ctx.available
        except Exception:  # noqa: BLE001 - 设备离线等降级不把刷新端点打成 500
            summary_refreshed = False
    return {"code": 0, "data": {"workspace_id": workspace_id, "cache_invalidated": True, "summary_refreshed": summary_refreshed}}


@router.delete("/{workspace_id}")
async def delete_workspace(workspace_id: str, payload: dict = Depends(require_auth)):
    """删除工作区注册（含历史遗留的服务端文件镜像，如 staging/snapshots）。"""
    try:
        return {"code": 0, "data": workspaces.delete_workspace(payload["sub"], workspace_id)}
    except LookupError as exc:
        raise NotFoundException(str(exc)) from exc
