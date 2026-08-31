"""普通聊天的 token 滑动窗口淘汰。

服务端仅保存最近的热原文窗口：达到触发阈值后，先由分层记忆任务为可淘汰
前缀生成 L1 段摘要，再物理删除最早消息及其附件。客户端本地历史不受影响。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from loguru import logger
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import get_redis
from app.models.db_models import Attachment, Conversation, ConversationMemoryState, Message
from app.services.content_codec import normalize_content
from app.services.usage import estimate_tokens


CONTEXT_KEY = "conv:ctx:{conversation_id}"
EXTRACT_OFFSET_KEY = "mem:extract_offset:{conversation_id}"


def _message_tokens(message: Message) -> int:
    return estimate_tokens(normalize_content(message.content))


def select_messages_to_evict(messages: list[Message]) -> list[Message]:
    """返回超过触发阈值后应从最旧端淘汰的消息。

    保留最新 ``CONVERSATION_SUMMARY_KEEP_TOKENS`` 左右的完整消息；若最后一条
    单独超过预算，仍保留该消息，避免切断一条用户输入或模型输出。
    """
    total = sum(_message_tokens(message) for message in messages)
    if total < settings.CONVERSATION_SUMMARY_TRIGGER_TOKENS:
        return []

    keep_budget = settings.CONVERSATION_SUMMARY_KEEP_TOKENS
    kept = 0
    first_kept_index = len(messages)
    for index in range(len(messages) - 1, -1, -1):
        cost = _message_tokens(messages[index])
        if first_kept_index < len(messages) and kept + cost > keep_budget:
            break
        kept += cost
        first_kept_index = index
    return messages[:first_kept_index]


def select_safe_evictable_messages(
    candidates: list[Message],
    processed_message_count: int,
) -> list[Message]:
    """仅淘汰完整且已生成 L1 摘要的段，不能从摘要段中间切开。"""
    segment_size = max(2, settings.CONVERSATION_SEGMENT_ROUNDS * 2)
    complete_count = min(len(candidates), max(0, processed_message_count))
    complete_count -= complete_count % segment_size
    return candidates[:complete_count]


async def _delete_attachment_files(user_id: str, file_urls: list[str]) -> None:
    """删除已淘汰消息的聊天附件，且只允许用户自己的上传目录。"""
    base = (Path(settings.UPLOAD_DIR) / "chat" / str(user_id)).resolve()
    for url in file_urls:
        if not url:
            continue
        parts = [part for part in url.split("/") if part]
        if len(parts) < 3 or parts[0] != "uploads" or parts[1] != str(user_id):
            logger.warning("[ConversationTrim] 跳过非当前用户聊天附件: {}", url)
            continue
        target = (base / parts[-1]).resolve()
        if not target.is_relative_to(base):
            logger.warning("[ConversationTrim] 附件路径越界，跳过: {}", url)
            continue
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("[ConversationTrim] 附件删除失败: {} err={}", target, exc)


async def _trim_redis_context(conversation_id: str, removed: int) -> None:
    """按 Redis 自身内容维护热窗口，避免 Redis 过期后误裁剪新消息。"""
    if removed <= 0:
        return
    try:
        redis = get_redis()
        context_key = CONTEXT_KEY.format(conversation_id=conversation_id)
        raw_messages = await redis.lrange(context_key, 0, -1)
        messages: list[dict] = []
        for raw in raw_messages:
            try:
                item = raw if isinstance(raw, dict) else json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(item, dict):
                messages.append(item)
        total = sum(estimate_tokens(normalize_content(str(item.get("content") or ""))) for item in messages)
        redis_removed = 0
        if total >= settings.CONVERSATION_SUMMARY_TRIGGER_TOKENS:
            retained = 0
            first_kept = len(messages)
            for index in range(len(messages) - 1, -1, -1):
                cost = estimate_tokens(normalize_content(str(messages[index].get("content") or "")))
                if first_kept < len(messages) and retained + cost > settings.CONVERSATION_SUMMARY_KEEP_TOKENS:
                    break
                retained += cost
                first_kept = index
            redis_removed = first_kept
            if redis_removed:
                await redis.ltrim(context_key, redis_removed, -1)

        offset_key = EXTRACT_OFFSET_KEY.format(conversation_id=conversation_id)
        raw_offset = await redis.get(offset_key)
        if raw_offset is not None and redis_removed:
            try:
                offset = max(0, int(raw_offset) - redis_removed)
            except (TypeError, ValueError):
                offset = 0
            await redis.set(offset_key, str(offset), ex=604800)
    except Exception as exc:  # noqa: BLE001
        # 数据库删除已提交时不回滚；下轮上下文写入或清理可再次校正 Redis。
        logger.warning("[ConversationTrim] Redis 热窗口同步失败: conv={} err={}", conversation_id, exc)


async def trim_conversation_messages(session: AsyncSession, conversation_id: str) -> int:
    """按 token 预算物理淘汰最早的已摘要消息，返回删除条数。

    只删除 ``processed_message_count`` 覆盖的前缀，保证每段被删除原文已有
    L1 摘要与 L2 全局梗概。若异步摘要尚未完成，本轮宁可不删。
    """
    try:
        cid = uuid.UUID(str(conversation_id))
    except (ValueError, TypeError):
        return 0

    conv = await session.get(Conversation, cid)
    if conv is None or conv.scene != "chat":
        return 0
    if session.bind and session.bind.dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:conversation_key))"),
            {"conversation_key": str(cid)},
        )

    messages = (
        await session.execute(
            select(Message)
            .where(Message.conversation_id == cid)
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
    ).scalars().all()
    candidates = select_messages_to_evict(messages)
    if not candidates:
        return 0

    state = await session.get(ConversationMemoryState, cid)
    processed = max(0, state.processed_message_count) if state else 0
    candidates = select_safe_evictable_messages(candidates, processed)
    if not candidates:
        logger.info(
            "[ConversationTrim] 摘要尚未覆盖完整待淘汰段，延后删除: conv={} processed={}",
            conversation_id,
            processed,
        )
        return 0

    old_ids = [message.id for message in candidates]
    attachments = (
        await session.execute(select(Attachment).where(Attachment.message_id.in_(old_ids)))
    ).scalars().all()
    await _delete_attachment_files(str(conv.user_id), [item.file_url for item in attachments])
    result = await session.execute(delete(Message).where(Message.id.in_(old_ids)))
    if state is not None:
        state.processed_message_count = max(0, processed - len(candidates))
    await session.commit()

    removed = result.rowcount or 0
    await _trim_redis_context(conversation_id, removed)
    logger.info(
        "[ConversationTrim] token 窗口滑动: conv={} removed_messages={} retained_target_tokens={}",
        conversation_id,
        removed,
        settings.CONVERSATION_SUMMARY_KEEP_TOKENS,
    )
    return removed


async def cleanup_all_conversations(session: AsyncSession) -> int:
    """每日兜底：补偿失败重试后仍未完成的 token 窗口淘汰。"""
    conversations = (
        await session.execute(select(Conversation.id).where(Conversation.scene == "chat"))
    ).scalars().all()
    trimmed = 0
    for cid in conversations:
        trimmed += await trim_conversation_messages(session, str(cid))
    return trimmed
