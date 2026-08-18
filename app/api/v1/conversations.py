"""对话模块 API —— 会话管理（服务端存储，支撑多端同步）.

设计要点：
  - 双端存储：本地 SQLite（前端）+ PostgreSQL（服务端），数据量一致
  - 用户隔离：所有会话按 user_id 归属，只能访问自己的会话
  - 幂等：客户端会话 ID / 消息 ID 唯一（部分唯一索引），重复提交直接重放
  - 并发：同一会话加 Redis 互斥锁，多端并发消息串行处理保证顺序
  - 游客：不落库（无账号），仍走 Redis 上下文，逻辑不变
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import async_session_factory, get_db
from app.core.deps import get_current_user, require_auth
from app.core.exceptions import BadRequestException, ForbiddenException, NotFoundException, UnauthorizedException
from app.core.redis import get_redis
from app.models.conversation import (
    CreateConversationRequest,
    SendMessageRequest,
    UpdateConversationRequest,
)
from app.models.db_models import Attachment, Conversation, Message
from app.services.content_codec import normalize_content, serialize_content, split_segments
from app.services.orchestrator import orchestrator
from app.services.speech import detect_audio_meta, save_audio_file, synthesize_speech

router = APIRouter()

CONV_LOCK_TTL = 180  # 会话级并发锁超时（秒）
CONV_EVENTS_CHANNEL = "conv:events"  # Redis 频道：会话实时事件（SSE 多端同步）

# 活跃 TTS 任务（按 user_id 跟踪）：用户发送下一条消息时中断上一条语音合成
_active_tts_tasks: dict[str, asyncio.Task] = {}


# ── 工具 ─────────────────────────────────────────────

def _uid(payload: dict) -> uuid.UUID | None:
    """登录用户 UUID；游客返回 None."""
    sub = payload.get("sub")
    if not sub:
        return None
    try:
        return uuid.UUID(sub)
    except (ValueError, TypeError):
        return None


def _message_steps(message: Message | None) -> list[dict]:
    """兼容历史消息：metadata 非法或不是对象时返回空步骤."""
    if not message or not message.metadata_:
        return []
    try:
        data = json.loads(message.metadata_)
        steps = data.get("steps") if isinstance(data, dict) else None
        return steps if isinstance(steps, list) else []
    except (ValueError, TypeError):
        return []


async def _get_owned_conversation(
    db: AsyncSession,
    conversation_id: str,
    uid: uuid.UUID | None,
) -> Conversation | None:
    """获取当前用户会话；游客或找不到返回 None（游客不落库）。"""
    if uid is None:
        return None
    try:
        conv_uuid = uuid.UUID(conversation_id)
    except (ValueError, TypeError):
        return None
    conv = await db.get(Conversation, conv_uuid)
    if not conv or conv.is_deleted:
        return None
    if conv.user_id != uid:
        raise ForbiddenException("无权访问该会话")
    return conv


async def _acquire_conv_lock(conversation_id: str):
    """会话级互斥锁：同一会话的并发消息串行处理，保证顺序与上下文一致."""
    r = get_redis()
    lock = r.lock(f"conv:lock:{conversation_id}", timeout=CONV_LOCK_TTL)
    acquired = await lock.acquire(blocking=True, blocking_timeout=15)
    if not acquired:
        raise BadRequestException("会话繁忙，请稍后重试")
    return lock


async def _find_duplicate(db: AsyncSession, conversation_id: str, client_message_id: str) -> dict | None:
    """幂等重放：同会话同客户端消息 ID 已处理且有回复 → 返回已存结果."""
    try:
        conv_uuid = uuid.UUID(conversation_id)
    except (ValueError, TypeError):
        return None
    user_msg = (
        await db.execute(
            select(Message).where(
                Message.conversation_id == conv_uuid,
                Message.client_message_id == client_message_id,
            )
        )
    ).scalar_one_or_none()
    if not user_msg:
        return None
    # 用户消息 metadata 里记录 reply_message_id（同事务写入 created_at 相同，不能用时间排序）
    reply_id = None
    if user_msg.metadata_:
        try:
            reply_id = json.loads(user_msg.metadata_).get("reply_message_id")
        except (ValueError, TypeError):
            reply_id = None
    assistant = None
    if reply_id:
        try:
            assistant = await db.get(Message, uuid.UUID(reply_id))
        except (ValueError, TypeError):
            assistant = None
    if not assistant:
        return None  # 用户消息已存但回复未完成（中断场景）→ 重新处理
    return {
        "message_id": str(assistant.id),
        "content": normalize_content(assistant.content),
        "segments": split_segments(assistant.content),
        "citations": json.loads(assistant.citations) if assistant.citations else [],
        "steps": _message_steps(assistant),
        "scene": None,
        "local_mode": False,
        "replayed": True,
    }


async def _persist_messages(db: AsyncSession, conv: Conversation, req: SendMessageRequest, result: dict) -> None:
    """保存用户消息 + AI 回复到 PostgreSQL（幂等由唯一索引兜底）."""
    # 重新生成/中断重试：同一 client_message_id 已存在 → 复用旧用户消息行，避免重复 & 保证双端 ID 一致
    existing_user = None
    if req.message_id:
        existing_user = (
            await db.execute(
                select(Message).where(
                    Message.conversation_id == conv.id,
                    Message.client_message_id == req.message_id,
                )
            )
        ).scalar_one_or_none()
    if existing_user is not None:
        user_msg = existing_user
        user_msg.content = req.display_content if req.display_content is not None else req.content
    else:
        user_msg = Message(
            id=uuid.uuid4(),
            conversation_id=conv.id,
            role="user",
            content=req.display_content if req.display_content is not None else req.content,
            client_message_id=req.message_id,
            metadata_=json.dumps({"guest_id": req.guest_id}, ensure_ascii=False) if req.guest_id else None,
        )
        db.add(user_msg)
    try:
        assistant_msg = Message(
            id=uuid.UUID(result["message_id"]),
            conversation_id=conv.id,
            role="assistant",
            # assistant 内容按"一次交互"存储：content 为 JSON 数组（短句合并），
            # 后续"多条短句回复"策略直接向数组追加分段，不新增消息行。
            content=serialize_content(result.get("segments") or result.get("content", "")),
            citations=json.dumps(result.get("citations") or [], ensure_ascii=False),
            metadata_=json.dumps({"steps": result.get("steps") or []}, ensure_ascii=False),
        )
        db.add(assistant_msg)
    except (ValueError, TypeError, KeyError):
        assistant_msg = Message(
            id=uuid.uuid4(),
            conversation_id=conv.id,
            role="assistant",
            content=serialize_content(result.get("segments") or result.get("content", "")),
            citations=json.dumps(result.get("citations") or [], ensure_ascii=False),
            metadata_=json.dumps({"steps": result.get("steps") or []}, ensure_ascii=False),
        )
        db.add(assistant_msg)
        result["message_id"] = str(assistant_msg.id)

    # 记录用户消息 → 回复的关联（幂等重放用）
    meta = {"reply_message_id": str(assistant_msg.id)}
    if req.guest_id:
        meta["guest_id"] = req.guest_id
    user_msg.metadata_ = json.dumps(meta, ensure_ascii=False)

    # 用户消息附件（图片/语音/视频）：当前只持久化引用，语音转文字后续接入
    # 复用旧用户消息时不重复写入附件（原附件仍挂在同一行上）
    if existing_user is None:
        for att in req.attachments or []:
            if not isinstance(att, dict) or not att.get("url"):
                continue
            db.add(
                Attachment(
                    message_id=user_msg.id,
                    type=att.get("type") or "file",
                    file_url=str(att["url"])[:500],
                    file_name=att.get("name"),
                    file_size=att.get("size"),
                    mime_type=att.get("mime_type"),
                )
            )

    # 首条消息返回的标题落库；同时刷新排序时间
    if result.get("title"):
        conv.title = result["title"]
    conv.updated_at = datetime.now(timezone.utc)
    try:
        await db.commit()
    except IntegrityError:
        # 唯一索引冲突 → 说明该消息已被并发提交，回滚即可
        await db.rollback()
        return

    # 聊天记录生命周期：超过硬上限 → 异步裁剪（物理删除最旧消息 + 附件文件）
    over = (
        await db.execute(
            select(func.count()).select_from(Message).where(Message.conversation_id == conv.id)
        )
    ).scalar_one()
    if over > settings.CONVERSATION_MESSAGE_HARD_CAP:
        try:
            from celery_app.tasks import trim_conversation_messages as trim_task

            trim_task.delay(str(conv.id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("消息裁剪入队失败: {}", exc)

    # 实时同步：向订阅端（桌面/网页/移动端）推送新消息事件
    user_attachments = [
        {
            "type": att.get("type") or "file",
            "url": str(att["url"]),
            "name": att.get("name"),
            "size": att.get("size"),
            "mime_type": att.get("mime_type"),
        }
        for att in (req.attachments or [])
        if isinstance(att, dict) and att.get("url")
    ]
    await _publish_conv_event(
        str(conv.user_id),
        "message.new",
        {
            "conversation_id": str(conv.id),
            "message_id": str(user_msg.id),
            "client_message_id": req.message_id,
            "role": "user",
            "content": req.content,
            "citations": [],
            "attachments": user_attachments,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    await _publish_conv_event(
        str(conv.user_id),
        "message.new",
        {
            "conversation_id": str(conv.id),
            "message_id": result.get("message_id"),
            "client_message_id": None,
            "role": "assistant",
            "content": result.get("content", ""),
            "segments": result.get("segments") or [],
            "citations": result.get("citations") or [],
            "steps": result.get("steps") or [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    if result.get("title"):
        await _publish_conv_event(
            str(conv.user_id),
            "conversation.updated",
            {"conversation_id": str(conv.id), "title": result["title"]},
        )


async def _publish_conv_event(user_id: str, event_type: str, data: dict) -> None:
    """向 Redis 频道发布会话事件（SSE 订阅端按 user_id 过滤）."""
    r = get_redis()
    try:
        await r.publish(
            CONV_EVENTS_CHANNEL,
            json.dumps({"user_id": user_id, "type": event_type, "data": data}, ensure_ascii=False),
        )
    except Exception as e:
        logger.warning("发布会话事件失败: {}", e)


# ── AI 回复异步 TTS（可中断） ─────────────────────────

def _cancel_user_tts(user_id: str) -> None:
    """中断该用户正在进行的语音合成任务."""
    task = _active_tts_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()


async def _run_tts_task(user_id: str, conversation_id: str, message_id: str, text: str) -> None:
    """异步把 AI 回复转成语音：合成 → 存文件 → 挂附件落库 → 推送 audio_ready."""
    try:
        audio = await synthesize_speech(text)
        ext, mime = detect_audio_meta(audio)
        _, url = save_audio_file(user_id, audio, ext=ext)

        # 给 AI 消息挂音频附件（消息此时已由 _persist_messages 落库）
        async with async_session_factory() as db:
            msg = await db.get(Message, uuid.UUID(message_id))
            if msg is None:
                logger.warning("TTS 完成但消息不存在，跳过附件: {}", message_id)
                return
            db.add(
                Attachment(
                    message_id=msg.id,
                    type="audio",
                    file_url=url,
                    file_name=f"reply.{ext}",
                    file_size=len(audio),
                    mime_type=mime,
                )
            )
            await db.commit()

        # 推送语音就绪事件（前端据此显示听筒按钮）
        await _publish_conv_event(
            user_id,
            "message.audio_ready",
            {
                "conversation_id": conversation_id,
                "message_id": message_id,
                "url": url,
                "type": "audio",
            },
        )
        logger.debug("TTS 完成: msg={} url={}", message_id, url)
    except asyncio.CancelledError:
        logger.debug("TTS 任务被新消息中断: msg={}", message_id)
        raise
    except Exception as e:
        logger.warning("TTS 任务失败: {}", e)
    finally:
        _active_tts_tasks.pop(user_id, None)


# ── 会话 CRUD ────────────────────────────────────────

@router.post("")
async def create_conversation(
    req: CreateConversationRequest,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_auth),
):
    """创建新会话（客户端传 client_conversation_id 时幂等）."""
    uid = _uid(payload)
    if uid is None:
        raise UnauthorizedException("请先登录")

    client_id: uuid.UUID | None = None
    if req.client_conversation_id:
        try:
            client_id = uuid.UUID(req.client_conversation_id)
        except (ValueError, TypeError):
            client_id = None
        if client_id:
            existing = await db.get(Conversation, client_id)
            if existing and not existing.is_deleted and existing.user_id == uid:
                return {
                    "code": 0,
                    "data": {
                        "conversation_id": str(existing.id),
                        "title": existing.title or "新会话",
                        "scene": existing.scene,
                    },
                }

    conv = Conversation(
        id=client_id or uuid.uuid4(),
        user_id=uid,
        title=req.title or "新会话",
        scene=req.scene,
    )
    db.add(conv)
    await db.commit()
    return {"code": 0, "data": {"conversation_id": str(conv.id), "title": conv.title, "scene": conv.scene}}


@router.get("")
async def list_conversations(
    scene: str = Query(default="chat"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_auth),
):
    """获取我的会话列表（按更新时间倒序）."""
    uid = _uid(payload)
    if uid is None:
        raise UnauthorizedException("请先登录")

    count_sub = (
        select(func.count(Message.id))
        .where(Message.conversation_id == Conversation.id)
        .correlate(Conversation)
        .scalar_subquery()
    )
    stmt = (
        select(Conversation, count_sub.label("message_count"))
        .where(Conversation.user_id == uid, Conversation.is_deleted == False)  # noqa: E712
        .order_by(Conversation.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if scene and scene != "all":
        stmt = stmt.where(Conversation.scene == scene)

    rows = (await db.execute(stmt)).all()
    items = [
        {
            "conversation_id": str(c.id),
            "title": c.title,
            "scene": c.scene,
            "message_count": n,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        }
        for c, n in rows
    ]
    total = (
        await db.execute(
            select(func.count())
            .select_from(Conversation)
            .where(Conversation.user_id == uid, Conversation.is_deleted == False)  # noqa: E712
        )
    ).scalar()
    return {"code": 0, "data": {"items": items, "total": total, "limit": limit, "offset": offset}}


@router.patch("/{conversation_id}")
async def update_conversation_title(
    conversation_id: str,
    req: UpdateConversationRequest,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_auth),
):
    """更新会话标题."""
    conv = await _get_owned_conversation(db, conversation_id, _uid(payload))
    if conv is None:
        raise NotFoundException("会话不存在")
    conv.title = req.title
    await db.commit()
    return {"code": 0, "message": "已更新"}


@router.delete("/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_auth),
):
    """软删除会话（保留消息，可恢复），同时清理 Redis 上下文."""
    conv = await _get_owned_conversation(db, conversation_id, _uid(payload))
    if conv is None:
        raise NotFoundException("会话不存在")
    conv.is_deleted = True
    await db.commit()
    await orchestrator.clear_context(conversation_id)
    return {"code": 0, "message": "已删除"}


# ── 消息 ─────────────────────────────────────────────

async def _delete_replaced_pair(
    db: AsyncSession,
    conversation_id: str,
    replace_message_id: str | None,
    replace_client_message_id: str | None,
) -> None:
    """重新生成：删除被替换的旧 AI 回复，复用旧用户消息行（保证客户端/服务端 ID 一致）.

    旧 assistant 优先用 replace_message_id；缺省时从旧用户消息 metadata.reply_message_id 解析
    （覆盖"回复失败但部分内容已落库"的重试场景）。用户消息行不删除，由 _persist_messages 复用。
    """
    if not replace_message_id and not replace_client_message_id:
        return
    try:
        conv_uuid = uuid.UUID(conversation_id)
    except (ValueError, TypeError):
        return

    old_user = None
    if replace_client_message_id:
        old_user = (
            await db.execute(
                select(Message).where(
                    Message.conversation_id == conv_uuid,
                    Message.client_message_id == replace_client_message_id,
                )
            )
        ).scalar_one_or_none()

    old_assistant_id = replace_message_id
    if not old_assistant_id and old_user is not None and old_user.metadata_:
        # 从旧用户消息的回复关联中解析旧 assistant（错误重试场景前端不知道服务端消息 ID）
        try:
            meta = json.loads(old_user.metadata_)
            old_assistant_id = meta.get("reply_message_id")
        except (ValueError, TypeError):
            old_assistant_id = None
    if old_assistant_id:
        try:
            old = await db.get(Message, uuid.UUID(str(old_assistant_id)))
        except (ValueError, TypeError):
            old = None
        if old is not None and old.conversation_id == conv_uuid and old.role == "assistant":
            await db.delete(old)
    await db.commit()


@router.post("/{conversation_id}/messages")
async def send_message(
    request: Request,
    conversation_id: str,
    req: SendMessageRequest,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user),
):
    """发送消息：会话锁串行处理 + 幂等重放 + 服务端持久化."""
    # 带了 token 但无效/过期 → 仍要求登录（让前端走刷新/重登流程，而不是静默变游客）
    if request.headers.get("authorization") and not payload:
        raise UnauthorizedException("登录已过期，请重新登录")

    user_id = payload.get("sub") or req.guest_id or "guest"
    is_guest = not payload
    uid = _uid(payload)
    # BYOK：用户自备 API key 每次请求临时携带，用完即弃（不落库、不打印日志）
    llm_api_key = request.headers.get("x-llm-api-key") or None

    # 用户发送新消息 → 中断上一条未完成的语音合成
    if not is_guest:
        _cancel_user_tts(user_id)

    lock = await _acquire_conv_lock(conversation_id)
    try:
        # 重新生成：先删除旧消息对（assistant + user），服务端保持单份、多端同步不重复
        if not is_guest and req.regenerate:
            await _delete_replaced_pair(
                db,
                conversation_id,
                req.replace_message_id,
                req.replace_client_message_id,
            )
        # 幂等：登录用户重复提交同一客户端消息 → 直接重放已存结果
        if not is_guest and req.message_id:
            replay = await _find_duplicate(db, conversation_id, req.message_id)
            if replay is not None:
                return {"code": 0, "data": replay}

        result = await orchestrator.handle_message(
            user_id=user_id,
            conversation_id=conversation_id,
            content=req.content,
            scene=req.scene,
            local_mode=req.local_mode,
            retrieval_query=req.retrieval_query,
            attachments=req.attachments,
            office_docs=req.office_docs,
            web_search_enabled=req.web_search,
            llm_api_key=llm_api_key,
            thinking_mode=req.thinking_mode,
            reply_style=req.reply_style,
            user_role=payload.get("role", "user"),
        )

        # 服务端持久化（仅登录用户；游客保持 Redis-only）
        if not is_guest:
            conv = await _get_owned_conversation(db, conversation_id, uid)
            if conv is not None:
                await _persist_messages(db, conv, req, result)
                # 异步把 AI 回复转成语音（完成后推 audio_ready 事件；新消息到达会被中断）
                reply_text = (result.get("content") or "").strip()
                if settings.TTS_ENABLED and reply_text and result.get("message_id"):
                    task = asyncio.create_task(
                        _run_tts_task(
                            user_id,
                            conversation_id,
                            str(result["message_id"]),
                            reply_text,
                        )
                    )
                    _active_tts_tasks[user_id] = task

        return {"code": 0, "data": result}
    finally:
        try:
            await lock.release()
        except Exception:
            pass


@router.get("/{conversation_id}/messages")
async def get_messages(
    conversation_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    before_message_id: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_auth),
):
    """获取会话消息历史（按时间正序分页）."""
    conv = await _get_owned_conversation(db, conversation_id, _uid(payload))
    if conv is None:
        raise NotFoundException("会话不存在")
    stmt = (
        select(Message)
        .where(Message.conversation_id == conv.id)
        .order_by(Message.created_at.asc())
        .limit(limit)
    )
    if before_message_id:
        try:
            before = await db.get(Message, uuid.UUID(before_message_id))
            if before:
                stmt = stmt.where(Message.created_at < before.created_at)
        except (ValueError, TypeError):
            pass
    rows = (await db.execute(stmt)).scalars().all()
    # 批量加载消息附件
    attachments_by_message: dict[str, list[dict]] = {}
    if rows:
        att_rows = (
            await db.execute(
                select(Attachment).where(
                    Attachment.message_id.in_([m.id for m in rows])
                )
            )
        ).scalars().all()
        for a in att_rows:
            attachments_by_message.setdefault(str(a.message_id), []).append(
                {
                    "id": str(a.id),
                    "type": a.type,
                    "url": a.file_url,
                    "name": a.file_name,
                    "size": a.file_size,
                    "mime_type": a.mime_type,
                }
            )
    items = [
        {
            "message_id": str(m.id),
            "role": m.role,
            "content": normalize_content(m.content),
            "segments": split_segments(m.content),
            "client_message_id": m.client_message_id,
            "citations": json.loads(m.citations) if m.citations else [],
            "steps": _message_steps(m),
            "attachments": attachments_by_message.get(str(m.id), []),
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in rows
    ]
    return {"code": 0, "data": {"items": items, "has_more": len(items) == limit}}


@router.get("/stream")
async def conversation_stream(
    request: Request,
    payload: dict = Depends(require_auth),
):
    """会话实时事件流（SSE）—— 多端同时在线时推送新消息/标题变更.

    客户端通过 fetch 流式读取（Authorization 头），按 user_id 过滤 Redis 订阅事件。
    """
    uid = _uid(payload)
    if uid is None:
        raise UnauthorizedException("请先登录")

    async def event_gen():
        r = get_redis()
        pubsub = r.pubsub()
        await pubsub.subscribe(CONV_EVENTS_CHANNEL)
        try:
            yield "event: connected\ndata: {}\n\n"
            async for msg in pubsub.listen():
                if await request.is_disconnected():
                    break
                if msg["type"] != "message":
                    continue
                try:
                    event = json.loads(msg["data"])
                except (ValueError, TypeError):
                    continue
                if event.get("user_id") != str(uid):
                    continue
                yield f"event: {event.get('type', 'message')}\ndata: {msg['data']}\n\n"
        finally:
            await pubsub.unsubscribe(CONV_EVENTS_CHANNEL)
            await pubsub.aclose()

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/scenes")
async def list_scenes():
    """获取所有可用场景模式."""
    scenes = await orchestrator.list_scenes()
    return {"code": 0, "data": {"scenes": scenes}}
