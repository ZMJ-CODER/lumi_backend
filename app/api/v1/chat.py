"""聊天流式接口（SSE）—— 前端 /api/v1/chat/stream 契约.

事件格式（text/event-stream，每行 data: {json}）：
  {"type":"delta","content":"部分文本"}
  {"type":"done","message_id":"uuid","content":"完整文本","citations":[...],"title":"...","scene":"..."}
  {"type":"error","message":"...","status":500}
"""

import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.conversations import (
    _acquire_conv_lock,
    _active_tts_tasks,
    _cancel_user_tts,
    _delete_replaced_pair,
    _find_duplicate,
    _get_owned_conversation,
    _persist_messages,
    _run_tts_task,
    _uid,
)
from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.exceptions import UnauthorizedException
from app.models.conversation import SendMessageRequest
from app.services.orchestrator import orchestrator

router = APIRouter()


def _sse(obj: dict) -> str:
    """构造 SSE 事件行."""
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@router.post("/stream")
async def chat_stream(
    request: Request,
    req: SendMessageRequest,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user),
):
    """流式聊天：SSE 逐段返回 AI 回复；联网搜索由模型自主决策（工具调用）."""
    if request.headers.get("authorization") and not payload:
        raise UnauthorizedException("登录已过期，请重新登录")

    user_id = payload.get("sub") or req.guest_id or "guest"
    is_guest = not payload
    uid = _uid(payload)
    conversation_id = req.conversation_id or ""

    async def event_gen():
        result = None
        lock = None
        started = False
        full_text = ""
        try:
            lock = await _acquire_conv_lock(conversation_id)
            started = True

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
                    yield _sse(
                        {
                            "type": "done",
                            "message_id": replay.get("message_id"),
                            "content": replay.get("content", ""),
                            "citations": replay.get("citations") or [],
                            "scene": req.scene,
                            "title": "",
                        }
                    )
                    return

            async for evt in orchestrator.handle_message_stream(
                user_id=user_id,
                conversation_id=conversation_id,
                content=req.content,
                scene=req.scene,
                local_mode=req.local_mode,
                retrieval_query=req.retrieval_query,
                attachments=req.attachments,
            ):
                if evt["type"] == "delta":
                    full_text += evt["content"]
                if evt["type"] == "done":
                    result = evt
                yield _sse(evt)

            # 服务端持久化（仅登录用户；游客保持 Redis-only）
            if not is_guest and result:
                conv = await _get_owned_conversation(db, conversation_id, uid)
                if conv is not None:
                    persist_result = {
                        "message_id": result["message_id"],
                        "content": result.get("content", ""),
                        "citations": result.get("citations") or [],
                        "title": result.get("title") or "",
                    }
                    await _persist_messages(db, conv, req, persist_result)
                    # 异步 TTS（完成后推 audio_ready；新消息到达会被中断）
                    reply_text = (result.get("content") or "").strip()
                    if settings.TTS_ENABLED and reply_text and result.get("message_id"):
                        _cancel_user_tts(user_id)
                        task = asyncio.create_task(
                            _run_tts_task(
                                user_id,
                                conversation_id,
                                str(result["message_id"]),
                                reply_text,
                            )
                        )
                        _active_tts_tasks[user_id] = task
        except Exception as exc:  # noqa: BLE001
            logger.warning("流式聊天失败: {}", exc)
            # 流式中断：仍持久化用户消息 + 已生成的部分回复，避免多端同步丢失
            if started and not is_guest:
                try:
                    conv = await _get_owned_conversation(db, conversation_id, uid)
                    if conv is not None:
                        partial = {
                            "message_id": str(uuid.uuid4()),
                            "content": full_text or "",
                            "citations": [],
                            "title": "",
                        }
                        await _persist_messages(db, conv, req, partial)
                except Exception as persist_exc:  # noqa: BLE001
                    logger.warning("流式中断持久化失败: {}", persist_exc)
            yield _sse({"type": "error", "message": "服务器内部错误", "status": 500})
        finally:
            if lock is not None:
                try:
                    await lock.release()
                except Exception:  # noqa: BLE001
                    pass

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
