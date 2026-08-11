"""按需文字转语音接口（AI 气泡"语音"按钮）.

前端点击语音 → POST /api/v1/tts {text} → 返回音频字节（wav/mp3）。
走现有 synthesize_speech 链路（TTS_PROVIDER：local_qwen3 / dashscope / edge）。
"""

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.core.deps import require_auth
from app.core.exceptions import BadRequestException
from app.services.speech import detect_audio_meta, synthesize_speech

router = APIRouter()

MAX_TTS_CHARS = 2000


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1, description="要转成语音的文本")


@router.post("")
async def synthesize(req: TTSRequest, payload: dict = Depends(require_auth)):
    """把文本合成为语音并返回音频字节."""
    text = req.text.strip()
    if not text:
        raise BadRequestException("文本不能为空")
    if len(text) > MAX_TTS_CHARS:
        raise BadRequestException(f"文本过长（最多 {MAX_TTS_CHARS} 字）")
    try:
        audio = await synthesize_speech(text)
    except Exception as exc:  # noqa: BLE001
        raise BadRequestException(f"语音合成失败: {exc}") from exc
    _, mime = detect_audio_meta(audio)
    return Response(content=audio, media_type=mime)
