"""按需文字转语音接口（AI 气泡"语音"按钮 + 声音设置）.

前端点击语音 → POST /api/v1/tts {text, voice?, rate?, pitch?, reference_audio?} → 返回音频字节。
声音设置：音色 / 语速 / 音高 / 克隆音色（上传样本）。
"""

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.deps import require_auth
from app.core.exceptions import BadRequestException
from app.services.speech import detect_audio_meta, synthesize_speech

router = APIRouter()

MAX_TTS_CHARS = 2000


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1, description="要转成语音的文本")
    voice: str | None = Field(default=None, description="音色（覆盖默认；如 Cherry / zh-CN-XiaoxiaoNeural）")
    rate: str | None = Field(default=None, description="语速，edge 格式如 +10% / -10%")
    pitch: str | None = Field(default=None, description="音高，edge 格式如 +5Hz / -5Hz")
    reference_audio: str | None = Field(default=None, description="克隆音色参考音频（本地 qwen3-tts 支持）")


@router.post("")
async def synthesize(req: TTSRequest, payload: dict = Depends(require_auth)):
    """把文本合成为语音并返回音频字节."""
    text = req.text.strip()
    if not text:
        raise BadRequestException("文本不能为空")
    if len(text) > MAX_TTS_CHARS:
        raise BadRequestException(f"文本过长（最多 {MAX_TTS_CHARS} 字）")
    try:
        audio = await synthesize_speech(
            text,
            voice=req.voice,
            rate=req.rate,
            pitch=req.pitch,
            reference_audio=req.reference_audio,
        )
    except Exception as exc:  # noqa: BLE001
        raise BadRequestException(f"语音合成失败: {exc}") from exc
    _, mime = detect_audio_meta(audio)
    return Response(content=audio, media_type=mime)


@router.post("/voice-sample")
async def upload_voice_sample(
    file: UploadFile = File(...),
    payload: dict = Depends(require_auth),
):
    """上传克隆音色样本（音频），返回样本路径供 TTS reference_audio 使用."""
    content = await file.read()
    if not content:
        raise BadRequestException("音频为空")
    if len(content) > 10 * 1024 * 1024:
        raise BadRequestException("音频样本不能超过 10MB")
    ext = Path(file.filename or "sample.wav").suffix.lower() or ".wav"
    if ext not in (".wav", ".mp3", ".m4a", ".ogg", ".flac"):
        raise BadRequestException("仅支持 wav / mp3 / m4a / ogg / flac")
    user_id = str(payload["sub"])
    dest = Path(settings.UPLOAD_DIR) / "tts_voice" / user_id
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{uuid.uuid4().hex}{ext}"
    path.write_bytes(content)
    return {
        "code": 0,
        "data": {
            "reference_audio": str(path),
            "name": file.filename or "voice-sample",
            "size": len(content),
        },
    }
