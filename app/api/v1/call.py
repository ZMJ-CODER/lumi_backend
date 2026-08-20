"""语音通话接口 —— 复用 Whisper（ASR）+ 云端千问（回复）+ 流式 TTS.

  - /call/turn    阻塞式一轮通话：语音 → 文本 → TTS 音频（一次性返回）
  - /call/stream  流式通话：模型边输出短句边 TTS（豆包式实时语音），SSE 逐段返回音频
视频通话当前仅前端占位，后端暂不开放。
"""

import base64
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.deps import require_auth
from app.core.exceptions import BadRequestException
from app.core.llm import LLMClient
from app.services.content_codec import normalize_content, serialize_content
from app.services.orchestrator import orchestrator
from app.services.speech import detect_audio_meta, speech_to_text, synthesize_speech
from app.services.usage import CATEGORY_CHAT

router = APIRouter()

_CALL_CONTEXT_BUDGET_TOKENS = 12000  # 通话沿用当前会话的短期窗口（任务连贯即可）
_CALL_MAX_TEXT_CHARS = 2000
_CALL_SEGMENT_MIN_CHARS = 8   # 短句最小长度（避免 TTS 被切得太碎）
_CALL_SEGMENT_COMMA_MIN = 15  # 逗号断句最小长度（豆包式：出逗号短句即处理）
_CALL_SEGMENT_MAX_CHARS = 40  # 无断句时硬切阈值（控制单段延迟）


class CallTurnRequest(BaseModel):
    conversation_id: str | None = Field(default=None, description="会话 ID（空则每次独立）")
    audio_url: str | None = Field(default=None, description="语音附件 URL（优先）")
    text: str | None = Field(default=None, description="文本输入（无音频时使用）")
    scene: str = Field(default="chat")


def _call_model_cfg() -> dict:
    """通话回复模型：服务端默认千问文本模型。"""
    return {
        "base_url": settings.QWEN_BASE_URL.rstrip("/"),
        "api_key": settings.QWEN_API_KEY,
        "model": settings.QWEN_MODEL,
        "timeout": 120.0,
    }


async def _build_call_messages(
    user_id: str, conversation_id: str, transcript: str
) -> list[dict]:
    """组装通话上下文：角色提示词 + 会话短期窗口 + 当前输入."""
    history = await orchestrator.get_context(conversation_id)
    system_prompt = await orchestrator._get_system_prompt(user_id, "chat")
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    for msg in orchestrator._trim_history(history, _CALL_CONTEXT_BUDGET_TOKENS):
        messages.append(
            {"role": msg.get("role"), "content": normalize_content(msg.get("content") or "")}
        )
    messages.append({"role": "user", "content": transcript})
    return messages


async def _resolve_input(req: "CallTurnRequest") -> str:
    """语音转写或文本输入 → 有效文本."""
    if req.audio_url:
        transcript = await speech_to_text(req.audio_url)
    else:
        transcript = (req.text or "").strip()
    if not transcript:
        raise BadRequestException("未识别到有效的语音内容")
    if len(transcript) > _CALL_MAX_TEXT_CHARS:
        raise BadRequestException("语音内容过长")
    return transcript


def _take_call_segment(buffer: str) -> str | None:
    """从流式缓冲中取出第一个完整短句：
    句末标点（。！？!?；;\n）优先；逗号且累积足够长也切；超长硬切。
    """
    for idx, ch in enumerate(buffer):
        if ch in "。！？!?；;\n" and idx + 1 >= _CALL_SEGMENT_MIN_CHARS:
            return buffer[: idx + 1]
        if ch in "，," and idx + 1 >= _CALL_SEGMENT_COMMA_MIN:
            return buffer[: idx + 1]
    if len(buffer) >= _CALL_SEGMENT_MAX_CHARS:
        return buffer[:_CALL_SEGMENT_MAX_CHARS]
    return None


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False, default=str)}\n\n"


@router.post("/turn")
async def call_turn(req: CallTurnRequest, payload: dict = Depends(require_auth)):
    """一轮语音通话（阻塞）：语音/文本 → 千问回复 → TTS 语音一次性返回。"""
    user_id = str(payload["sub"])
    transcript = await _resolve_input(req)
    conversation_id = req.conversation_id or f"call-{user_id}"
    messages = await _build_call_messages(user_id, conversation_id, transcript)

    cfg = _call_model_cfg()
    llm = LLMClient()
    try:
        reply = await llm.chat(
            messages,
            base_url=cfg["base_url"],
            api_key=cfg["api_key"],
            model=cfg["model"],
            timeout=cfg["timeout"],
            usage_user_id=user_id,
            usage_category=CATEGORY_CHAT,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("语音通话模型调用失败: {}", exc)
        raise BadRequestException(f"语音通话模型调用失败: {exc}") from exc
    reply = (reply or "").strip()
    if not reply:
        raise BadRequestException("语音通话模型未返回内容")

    # 4. 上下文延续（计入一次交互，与聊天共用 Redis 上下文）
    now = datetime.now(timezone.utc).isoformat()
    await orchestrator.append_context(
        conversation_id, {"role": "user", "content": transcript, "timestamp": now}
    )
    await orchestrator.append_context(
        conversation_id,
        {
            "role": "assistant",
            "content": serialize_content(reply),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )
    await orchestrator._maybe_summarize_context(conversation_id, user_id, "chat")

    # 5. TTS 合成语音（复用现有链路，自动回退 edge-tts）
    try:
        audio = await synthesize_speech(reply)
    except Exception as exc:  # noqa: BLE001
        logger.warning("语音通话 TTS 失败（仅返回文本）: {}", exc)
        audio = b""
    _, mime = detect_audio_meta(audio) if audio else ("mp3", "audio/mpeg")

    return {
        "code": 0,
        "data": {
            "text": reply,
            "transcript": transcript,
            "audio_base64": base64.b64encode(audio).decode("ascii") if audio else "",
            "mime": mime,
            "conversation_id": conversation_id,
        },
    }


@router.post("/stream")
async def call_stream(req: CallTurnRequest, payload: dict = Depends(require_auth)):
    """流式语音通话（SSE）：DS 边输出短句边 TTS，逐段返回音频（豆包式实时语音）.

    事件：
      {"type":"segment","index":0,"text":"短句","audio_base64":"...","mime":"audio/mpeg"}
      {"type":"done","content":"完整回复"}
      {"type":"error","message":"..."}
    """
    user_id = str(payload["sub"])
    try:
        transcript = await _resolve_input(req)
    except BadRequestException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise BadRequestException(f"语音处理失败: {exc}") from exc

    conversation_id = req.conversation_id or f"call-{user_id}"
    messages = await _build_call_messages(user_id, conversation_id, transcript)
    cfg = _call_model_cfg()
    llm = LLMClient()

    async def event_gen():
        buffer = ""
        full_text = ""
        segment_idx = 0
        try:
            async for delta in llm.chat_stream(
                messages,
                base_url=cfg["base_url"],
                api_key=cfg["api_key"],
                model=cfg["model"],
                timeout=cfg["timeout"],
                usage_user_id=user_id,
                usage_category=CATEGORY_CHAT,
            ):
                if not delta:
                    continue
                buffer += delta
                full_text += delta
                # DS 每输出一个短句（含逗号断句）就开始 TTS，不等全文
                while True:
                    seg = _take_call_segment(buffer)
                    if not seg:
                        break
                    buffer = buffer[len(seg):]
                    audio = b""
                    try:
                        audio = await synthesize_speech(seg)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("流式 TTS 单段失败: {}", exc)
                    yield _sse(
                        {
                            "type": "segment",
                            "index": segment_idx,
                            "text": seg,
                            "audio_base64": base64.b64encode(audio).decode("ascii") if audio else "",
                            "mime": detect_audio_meta(audio)[1] if audio else "audio/mpeg",
                        }
                    )
                    segment_idx += 1
            # 收尾剩余缓冲
            if buffer.strip():
                audio = b""
                try:
                    audio = await synthesize_speech(buffer.strip())
                except Exception as exc:  # noqa: BLE001
                    logger.warning("流式 TTS 收尾失败: {}", exc)
                yield _sse(
                    {
                        "type": "segment",
                        "index": segment_idx,
                        "text": buffer.strip(),
                        "audio_base64": base64.b64encode(audio).decode("ascii") if audio else "",
                        "mime": detect_audio_meta(audio)[1] if audio else "audio/mpeg",
                    }
                )
            # 上下文延续（与聊天同一 Redis 上下文，计入一次交互）
            now = datetime.now(timezone.utc).isoformat()
            await orchestrator.append_context(
                conversation_id, {"role": "user", "content": transcript, "timestamp": now}
            )
            await orchestrator.append_context(
                conversation_id,
                {
                    "role": "assistant",
                    "content": serialize_content(full_text),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
            await orchestrator._maybe_summarize_context(conversation_id, user_id, "chat")
            yield _sse({"type": "done", "content": full_text, "conversation_id": conversation_id})
        except Exception as exc:  # noqa: BLE001
            logger.warning("流式语音通话失败: {}", exc)
            yield _sse({"type": "error", "message": f"语音通话失败: {exc}"})

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
