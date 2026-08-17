"""语音服务 —— 语音转文字（Whisper）+ 转写纠错 + 文字转语音（千问 qwen-tts）.

说明：
  - ASR 用本地 openai-whisper（首次调用自动下载模型），转写在子线程执行避免阻塞事件循环；
  - 转写纠错用千问 qwen-turbo（用户口音/同音字导致的效果差在这里修正）；
  - TTS 用千问云端 qwen-tts（dashscope SDK）。
    注意：本机安装的本地版 qwen3-tts 依赖 MLX，而 MLX 仅支持 Apple Silicon，
    Windows 上无法加载（DLL 报错），因此这里走云端 qwen-tts，复用 .env 的千问 API Key。
"""

import os
import threading
import uuid
from pathlib import Path

import httpx
from loguru import logger

from app.core.config import settings

_whisper_model = None
_whisper_lock = threading.Lock()


def _ensure_ffmpeg_on_path() -> None:
    """把 imageio-ffmpeg 自带的 ffmpeg 加入 PATH（whisper 解码音频依赖 ffmpeg 命令）."""
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        bin_dir = os.path.dirname(exe)
        if bin_dir and bin_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
    except Exception as e:  # noqa: BLE001 - 环境缺 ffmpeg 时让 whisper 自行报错
        logger.debug("imageio-ffmpeg 不可用: {}", e)


# ── Whisper 转写 ──────────────────────────────────────

def _get_whisper_model():
    """懒加载 whisper 模型（线程安全，首次调用会下载模型）."""
    global _whisper_model
    if _whisper_model is None:
        with _whisper_lock:
            if _whisper_model is None:
                _ensure_ffmpeg_on_path()
                import whisper

                _whisper_model = whisper.load_model(settings.WHISPER_MODEL)
                logger.info("✅ Whisper 模型加载完成: {}", settings.WHISPER_MODEL)
    return _whisper_model


def _transcribe_sync(file_path: str) -> str:
    """同步转写（在子线程中执行）."""
    _ensure_ffmpeg_on_path()
    model = _get_whisper_model()
    result = model.transcribe(
        file_path,
        language=settings.WHISPER_LANGUAGE or None,
        fp16=False,  # CPU 上禁用 fp16
    )
    return (result.get("text") or "").strip()


async def transcribe_audio(file_path: str) -> str:
    """语音转文字（子线程执行，不阻塞事件循环）."""
    try:
        from app.core.executors import run_in_compute

        return await run_in_compute(_transcribe_sync, file_path)
    except Exception as e:
        logger.warning("Whisper 转写失败: {}", e)
        return ""


# ── 转写纠错（qwen-turbo） ────────────────────────────

_CORRECT_SYSTEM_PROMPT = (
    "你是语音转写纠错助手。用户口音或识别问题会导致语音转写出现同音字、错别字、"
    "多字漏字等错误。请把转写文本修正为通顺、规范的中文，保持原意和口语风格，"
    "不要增删内容，不要解释。只输出修正后的文本。"
)


async def _qwen_chat(system_prompt: str, user_text: str, model: str, max_tokens: int = 512) -> str:
    """调用千问 OpenAI 兼容接口（复用 .env 的 QWEN_BASE_URL / API_KEY）."""
    async with httpx.AsyncClient(
        base_url=settings.QWEN_BASE_URL,
        headers={"Authorization": f"Bearer {settings.QWEN_API_KEY}"},
        timeout=120,
    ) as client:
        resp = await client.post(
            "/chat/completions",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.2,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return (data["choices"][0]["message"]["content"] or "").strip()


async def correct_transcript(text: str) -> str:
    """用 qwen-turbo 修正语音转写文本；失败时返回原文."""
    if not text:
        return text
    try:
        corrected = await _qwen_chat(_CORRECT_SYSTEM_PROMPT, text, settings.QWEN_TURBO_MODEL)
        return corrected or text
    except Exception as e:
        logger.warning("转写纠错失败，使用原文: {}", e)
        return text


# ── TTS（千问 cosyvoice 优先，edge-tts 兜底） ─────────

def _synthesize_sync(text: str, voice: str | None = None) -> bytes:
    """用千问 cosyvoice 同步合成语音（子线程执行），返回音频字节."""
    import dashscope
    from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer

    dashscope.api_key = settings.QWEN_API_KEY
    synthesizer = SpeechSynthesizer(
        model=settings.TTS_MODEL,
        voice=voice or settings.TTS_VOICE,
        format=(
            AudioFormat.MP3_24000HZ_MONO_256KBPS
            if settings.TTS_FORMAT == "mp3"
            else AudioFormat.WAV_24000HZ_MONO_16BIT
        ),
    )
    audio = synthesizer.call(text)
    if not audio:
        raise RuntimeError("TTS 未返回音频数据")
    return audio


async def _edge_tts(
    text: str,
    voice: str | None = None,
    rate: str | None = None,
    pitch: str | None = None,
) -> bytes:
    """用 edge-tts（微软免费）合成语音，支持音色/语速/音高.

    用户自定义音色可能对应当前提供者（如千问 Cherry）而非 edge-tts 音色，
    此时先按用户选择尝试一次，失败自动回退默认 edge 音色，保证功能可用。
    """
    import edge_tts

    candidates = list(dict.fromkeys([voice or settings.TTS_EDGE_VOICE, settings.TTS_EDGE_VOICE]))
    last_err: Exception | None = None
    for candidate in candidates:
        try:
            communicate = edge_tts.Communicate(
                text,
                voice=candidate,
                rate=rate or "+0%",
                pitch=pitch or "+0Hz",
            )
            audio = b""
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio += chunk["data"]
            if not audio:
                raise RuntimeError("edge-tts 未返回音频数据")
            return audio
        except Exception as exc:  # noqa: BLE001 - 音色不可用时降级重试
            last_err = exc
            logger.warning("edge-tts 音色 {} 合成失败: {}", candidate, exc)
    raise last_err or RuntimeError("edge-tts 合成失败")


async def _local_qwen3_tts(
    text: str,
    voice: str | None = None,
    rate: str | None = None,
    pitch: str | None = None,
    reference_audio: str | None = None,
) -> bytes:
    """调用本机/局域网部署的 qwen3-tts HTTP 服务（Linux/Apple Silicon 上运行）."""
    body: dict = {"text": text}
    if voice:
        body["voice"] = voice
    if rate:
        body["rate"] = rate
    if pitch:
        body["pitch"] = pitch
    if reference_audio:
        body["reference_audio"] = reference_audio
    async with httpx.AsyncClient(timeout=settings.TTS_LOCAL_TIMEOUT) as client:
        resp = await client.post(settings.TTS_LOCAL_URL, json=body)
        resp.raise_for_status()
        audio = resp.content
    if not audio:
        raise RuntimeError("本地 qwen3-tts 未返回音频数据")
    return audio


async def synthesize_speech(
    text: str,
    voice: str | None = None,
    rate: str | None = None,
    pitch: str | None = None,
    reference_audio: str | None = None,
) -> bytes:
    """文字转语音：按 TTS_PROVIDER 选择主提供者，失败自动回退 edge-tts.

    voice / rate / pitch / reference_audio 均为可选覆盖参数（来自前端声音设置）：
      - edge-tts 支持 voice/rate/pitch；
      - 本地 qwen3-tts 额外支持 reference_audio（克隆音色）；
      - 千问 cosyvoice 仅支持 voice（其余参数忽略）。
    """
    provider = settings.TTS_PROVIDER
    if provider == "local_qwen3":
        try:
            return await _local_qwen3_tts(text, voice, rate, pitch, reference_audio)
        except Exception as e:
            logger.warning("本地 qwen3-tts 失败，回退 edge-tts: {}", e)
    elif provider == "dashscope":
        try:
            from app.core.executors import run_in_compute

            return await run_in_compute(_synthesize_sync, text, voice)
        except Exception as e:
            # 常见原因：账号未开通 TTS 权限（引擎 418）等
            logger.warning("千问 TTS 失败，回退 edge-tts: {}", e)
    return await _edge_tts(text, voice, rate, pitch)


def detect_audio_meta(data: bytes) -> tuple[str, str]:
    """根据音频头识别 (扩展名, MIME)，兼容 wav / mp3."""
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav", "audio/wav"
    if data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return "mp3", "audio/mpeg"
    return "mp3", "audio/mpeg"


def save_audio_file(user_id: str, audio_bytes: bytes, ext: str = "mp3") -> tuple[Path, str]:
    """保存音频到 uploads/chat/{user_id}/，返回 (本地路径, 可访问 URL)."""
    file_id = uuid.uuid4()
    dest = Path(settings.UPLOAD_DIR) / "chat" / user_id / f"{file_id}.{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(audio_bytes)
    url = f"/uploads/{user_id}/{file_id}.{ext}"
    return dest, url


def resolve_upload_path(url: str) -> Path | None:
    """把 /uploads/{user_id}/{file} URL 解析回本地文件路径."""
    if not url or not url.startswith("/uploads/"):
        return None
    relative = url[len("/uploads/"):]
    path = Path(settings.UPLOAD_DIR) / "chat" / relative
    return path if path.exists() else None


# ── 对外便捷入口 ──────────────────────────────────────

async def speech_to_text(audio_url: str) -> str:
    """语音附件 URL → 转写 → 纠错 → 文本."""
    path = resolve_upload_path(audio_url)
    if path is None:
        logger.warning("语音附件文件不存在: {}", audio_url)
        return ""
    raw = await transcribe_audio(str(path))
    if not raw:
        return ""
    if settings.ASR_CORRECT_ENABLED:
        return await correct_transcript(raw)
    return raw
