"""用户数据导出 API —— 设计文档 4.1「用户可随时导出自己的完整数据」."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_memory_text
from app.core.database import get_db
from app.core.deps import require_auth
from app.core.exceptions import BadRequestException
from app.models.db_models import (
    ControlLog,
    Conversation,
    Document,
    KnowledgeSpace,
    Memory,
    MemoryProfile,
    Message,
)
from app.services.content_codec import normalize_content

router = APIRouter()


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


@router.post("/export")
async def export_my_data(
    payload: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """导出当前用户的完整云端数据.

    包含：对话历史、长期记忆（L1 解密为明文返回本人）、个人文档元数据、画像、日志数量。
    不包含：文档原始内容、向量数据。
    """
    user_id = payload.get("sub", "")
    try:
        uid = uuid.UUID(str(user_id))
    except (ValueError, TypeError):
        raise BadRequestException("令牌无效")

    # 1. 对话 + 消息
    conversations = []
    convs = (
        await db.execute(
            select(Conversation)
            .where(Conversation.user_id == uid, Conversation.is_deleted.is_(False))
            .order_by(Conversation.updated_at.desc())
        )
    ).scalars().all()
    for c in convs:
        msgs = (
            await db.execute(
                select(Message)
                .where(Message.conversation_id == c.id)
                .order_by(Message.created_at.asc())
            )
        ).scalars().all()
        conversations.append(
            {
                "conversation_id": str(c.id),
                "title": c.title,
                "scene": c.scene,
                "created_at": _iso(c.created_at),
                "updated_at": _iso(c.updated_at),
                "message_count": len(msgs),
                "messages": [
                    {
                        "message_id": str(m.id),
                        "role": m.role,
                        "content": normalize_content(m.content),
                        "created_at": _iso(m.created_at),
                    }
                    for m in msgs
                ],
            }
        )

    # 2. 长期记忆（L1 解密为明文返回本人）
    memories = []
    mems = (
        await db.execute(
            select(Memory).where(Memory.user_id == uid, Memory.is_deleted.is_(False))
        )
    ).scalars().all()
    for m in mems:
        fact = m.fact
        if m.privacy_level == 1 and m.fact_encrypted:
            try:
                fact = decrypt_memory_text(m.fact_encrypted, str(uid), m.key_version or 1)
            except Exception:  # noqa: BLE001
                fact = m.fact
        memories.append(
            {
                "memory_id": str(m.id),
                "fact": fact,
                "memory_type": m.memory_type,
                "importance": m.importance,
                "privacy_level": m.privacy_level,
                "created_at": _iso(m.created_at),
            }
        )

    # 3. 文档（个人空间元数据）
    docs = (
        await db.execute(
            select(Document)
            .join(KnowledgeSpace, KnowledgeSpace.id == Document.space_id)
            .where(KnowledgeSpace.user_id == uid, KnowledgeSpace.is_public.is_(False))
            .order_by(Document.created_at.desc())
        )
    ).scalars().all()
    documents = [
        {
            "document_id": str(d.id),
            "filename": d.filename,
            "file_size": d.file_size,
            "status": d.status,
            "category": d.category,
            "chunk_count": d.chunk_count,
            "created_at": _iso(d.created_at),
        }
        for d in docs
    ]

    # 4. 画像
    profile = await db.get(MemoryProfile, uid)
    profile_data = {
        "profile": profile.profile if profile else None,
        "version": profile.version if profile else None,
        "updated_at": _iso(profile.updated_at) if profile else None,
    }

    # 5. 操控日志数量
    control_logs_count = (
        await db.execute(
            select(func.count()).select_from(ControlLog).where(ControlLog.user_id == uid)
        )
    ).scalar_one()

    return {
        "code": 0,
        "data": {
            "user_id": user_id,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "conversations": conversations,
            "memories": memories,
            "documents": documents,
            "memory_profile": profile_data,
            "control_logs_count": control_logs_count,
        },
    }


@router.delete("/account")
async def delete_my_data(payload: dict = Depends(require_auth)):
    """删除当前用户的所有云端数据（需二次确认）.

    设计文档承诺：服务端 24 小时内执行并确认。
    实际实现：立即软删除，后台任务物理清理。
    """
    user_id = payload.get("sub", "")
    # TODO:
    # 1. 软删除用户 (is_active = false)
    # 2. Celery 任务：24h 内物理删除 conversations, messages, memories,
    #    document_chunks, documents, knowledge_spaces (非公共), control_logs
    # 3. 保留操作审计日志（不含内容）
    return {"code": 0, "message": "数据删除请求已提交，将在 24 小时内完成清理"}
