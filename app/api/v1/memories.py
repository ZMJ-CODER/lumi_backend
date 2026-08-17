"""长期记忆调试接口（仅 superadmin，见设计文档 §9）.

挂载路径：/api/v1/admin/memories
用户不可自管理记忆；本组接口仅供开发者排障使用。
"""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_superadmin
from app.core.exceptions import BadRequestException, NotFoundException
from app.models.db_models import Memory, MemoryProfile
from app.models.memory import UpdateMemoryRequest
from app.services.rag.embeddings import embed_texts

router = APIRouter()


def _memory_dict(m: Memory) -> dict:
    return {
        "memory_id": str(m.id),
        "fact": m.fact,  # L1 为脱敏占位符，绝不含密文/明文
        "memory_type": m.memory_type,
        "privacy_level": m.privacy_level,
        "importance": m.importance,
        "confidence": m.confidence,
        "is_deleted": m.is_deleted,
        "superseded_by": str(m.superseded_by) if m.superseded_by else None,
        "access_count": m.access_count,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def _to_uid(user_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(user_id))
    except (ValueError, TypeError):
        raise BadRequestException("user_id 无效") from None


@router.get("")
async def list_memories(
    user_id: str = Query(..., description="目标用户 ID"),
    include_deleted: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_superadmin),
):
    """列出某用户的长期记忆（L1 只返回占位符）."""
    uid = _to_uid(user_id)
    base = select(Memory).where(Memory.user_id == uid)
    if not include_deleted:
        base = base.where(Memory.is_deleted.is_(False))
    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    rows = (
        await db.execute(base.order_by(Memory.created_at.desc()).limit(limit).offset(offset))
    ).scalars().all()
    return {
        "code": 0,
        "data": {"items": [_memory_dict(m) for m in rows], "total": total},
    }


@router.patch("/{memory_id}")
async def update_memory(
    memory_id: str,
    req: UpdateMemoryRequest,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_superadmin),
):
    """编辑记忆事实（L1 隐私记忆仅允许改类型/重要度，不允许改内容）."""
    try:
        mid = uuid.UUID(memory_id)
    except (ValueError, TypeError):
        raise BadRequestException("memory_id 无效") from None
    mem = await db.get(Memory, mid)
    if not mem:
        raise NotFoundException("记忆不存在")
    if mem.privacy_level == 1 and req.content is not None:
        raise BadRequestException("L1 隐私记忆不支持直接编辑内容，请通过对话抽取重建")
    if req.content is not None:
        content = req.content.strip()
        mem.fact = content
        try:
            embedding = (await embed_texts([content]))[0]
            mem.embedding = embedding
        except Exception:  # noqa: BLE001
            pass  # 向量更新失败不阻塞编辑，旧向量仍可用于检索
    if req.memory_type is not None:
        mem.memory_type = req.memory_type
    if req.importance is not None:
        mem.importance = req.importance
    if req.confidence is not None:
        mem.confidence = req.confidence
    await db.commit()
    return {"code": 0, "message": "已更新"}


@router.delete("/{memory_id}")
async def delete_memory(
    memory_id: str,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_superadmin),
):
    """物理删除一条记忆."""
    try:
        mid = uuid.UUID(memory_id)
    except (ValueError, TypeError):
        raise BadRequestException("memory_id 无效") from None
    mem = await db.get(Memory, mid)
    if not mem:
        raise NotFoundException("记忆不存在")
    await db.delete(mem)
    await db.commit()
    return {"code": 0, "message": "已物理删除"}


@router.get("/{user_id}/profile")
async def get_user_profile(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_superadmin),
):
    """查看某用户画像（含版本与更新时间）."""
    uid = _to_uid(user_id)
    profile = await db.get(MemoryProfile, uid)
    if not profile:
        raise NotFoundException("该用户暂无画像")
    return {
        "code": 0,
        "data": {
            "user_id": user_id,
            "profile": profile.profile,
            "version": profile.version,
            "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
        },
    }
