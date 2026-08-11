"""聊天记录生命周期：按条数上限物理裁剪消息与附件文件（见设计文档 §8.3）.

规则：每会话保留最近 CONVERSATION_MESSAGE_KEEP 条（默认 50），
超过 CONVERSATION_MESSAGE_HARD_CAP（默认 70）时触发裁剪，
物理删除最旧消息 + 附件行（FK CASCADE）+ 附件磁盘文件（路径校验）。
"""

import uuid
from pathlib import Path

from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.db_models import Attachment, Conversation, Message


async def _delete_attachment_files(session: AsyncSession, user_id: str, file_urls: list[str]) -> None:
    """删除聊天附件磁盘文件（路径校验：必须位于 uploads/chat/{user_id}/ 内）."""
    base = (Path(settings.UPLOAD_DIR) / "chat" / str(user_id)).resolve()
    for url in file_urls:
        if not url:
            continue
        parts = [p for p in url.split("/") if p]
        # file_url 形如 /uploads/{user_id}/{filename}（静态挂载目录即 UPLOAD_DIR/chat）
        if len(parts) < 3 or parts[0] != "uploads":
            logger.warning("[Trim] 跳过非聊天附件路径: {}", url)
            continue
        if parts[1] != str(user_id):
            logger.warning("[Trim] 附件不属于当前用户，跳过: {}", url)
            continue
        target = (base / parts[-1]).resolve()
        if not target.is_relative_to(base):
            logger.warning("[Trim] 附件路径越界，跳过: {}", url)
            continue
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:  # noqa: BLE001
            logger.warning("[Trim] 附件删除失败: {} err={}", target, exc)


async def trim_conversation_messages(session: AsyncSession, conversation_id: str) -> int:
    """物理删除超出 KEEP 上限的最旧消息 + 附件文件。返回删除条数."""
    try:
        cid = uuid.UUID(str(conversation_id))
    except (ValueError, TypeError):
        return 0
    conv = await session.get(Conversation, cid)
    if not conv:
        return 0

    keep = settings.CONVERSATION_MESSAGE_KEEP
    total = (
        await session.execute(
            select(func.count()).select_from(Message).where(Message.conversation_id == cid)
        )
    ).scalar_one()
    if total <= keep:
        return 0

    # 最旧的 (total - keep) 条：按 (created_at, id) 稳定排序
    stmt = (
        select(Message)
        .where(Message.conversation_id == cid)
        .order_by(Message.created_at.asc(), Message.id.asc())
        .limit(total - keep)
    )
    old_messages = (await session.execute(stmt)).scalars().all()
    if not old_messages:
        return 0
    old_ids = [m.id for m in old_messages]

    # 先删附件磁盘文件（防越权路径校验），再删消息行（附件行随 FK CASCADE 清理）
    atts = (
        await session.execute(select(Attachment).where(Attachment.message_id.in_(old_ids)))
    ).scalars().all()
    await _delete_attachment_files(session, str(conv.user_id), [a.file_url for a in atts])

    result = await session.execute(delete(Message).where(Message.id.in_(old_ids)))
    await session.commit()
    removed = result.rowcount or 0
    logger.debug("[Trim] 会话 {} 裁剪 {} 条（保留 {}）", conversation_id, removed, keep)
    return removed


async def cleanup_all_conversations(session: AsyncSession) -> int:
    """每日兜底：扫描所有超过硬上限的会话并逐个裁剪。返回裁剪条数."""
    cap = settings.CONVERSATION_MESSAGE_HARD_CAP
    rows = (
        await session.execute(
            select(Conversation.id)
            .join(Message, Message.conversation_id == Conversation.id)
            .group_by(Conversation.id)
            .having(func.count(Message.id) > cap)
        )
    ).scalars().all()
    trimmed = 0
    for cid in rows:
        trimmed += await trim_conversation_messages(session, str(cid))
    return trimmed
