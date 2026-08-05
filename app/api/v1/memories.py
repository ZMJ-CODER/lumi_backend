"""记忆模块 API."""

from fastapi import APIRouter, Depends, Query

from app.core.deps import require_auth
from app.models.memory import MemorySettingsRequest, UpdateMemoryRequest

router = APIRouter()


@router.get("")
async def list_memories(
    limit: int = Query(default=50, ge=1, le=200),
    payload: dict = Depends(require_auth),
):
    """获取我的长期记忆列表."""
    # TODO: 分页查询记忆
    return {"code": 0, "data": {"items": [], "total": 0}}


@router.patch("/{memory_id}")
async def update_memory(memory_id: str, req: UpdateMemoryRequest, payload: dict = Depends(require_auth)):
    """编辑记忆."""
    return {"code": 0, "message": "已更新"}


@router.delete("/{memory_id}")
async def delete_memory(memory_id: str, payload: dict = Depends(require_auth)):
    """删除记忆."""
    return {"code": 0, "message": "已删除"}


@router.put("/settings")
async def update_memory_settings(req: MemorySettingsRequest, payload: dict = Depends(require_auth)):
    """记忆过期规则设置."""
    return {"code": 0, "data": {"auto_expire_days": req.auto_expire_days, "cleanup_threshold": req.cleanup_threshold}}
