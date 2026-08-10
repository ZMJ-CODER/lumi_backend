"""Qwen3-TTS 本地 HTTP 服务（在 Linux / Apple Silicon 上运行）.

背景：qwen3-tts 基于 MLX，仅支持 Apple Silicon / Linux（有可用 MLX 构建），
Windows 上无法加载。本脚本把本地 TTS 包装成 HTTP 服务，后端通过
TTS_LOCAL_URL 调用（例如 Linux 上跑本服务，Windows 后端指向 http://<linux-ip>:8765/tts）。

引擎策略（--engine）：
  auto  （默认）：优先 qwen3（MLX），不可用（缺 libmlx.so / 导入失败）自动回退 edge-tts；
  qwen3          ：只用 MLX qwen3；
  edge           ：只用 edge-tts。

Linux 用法：
  pip install qwen3-tts fastapi uvicorn
  python tools/qwen3_tts_server.py --port 8765
  首次调用会自动下载模型（mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-bf16）

接口：
  GET  /health           → {"status": "ok"}
  POST /tts {"text": "..."}  → audio/wav 二进制
"""

import argparse
import asyncio
import tempfile
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

app = FastAPI(title="Qwen3-TTS Local Server")

DEFAULT_MODEL_ID = "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-bf16"
DEFAULT_SPEAKER = "serena"
DEFAULT_LANGUAGE = "chinese"
DEFAULT_SAMPLE_RATE = 24000
ENGINE = "auto"  # auto / qwen3 / edge（命令行 --engine 覆盖）

_model = None
_model_lock = threading.Lock()


class TTSRequest(BaseModel):
    text: str
    speaker: str | None = None
    language: str | None = None


def _qwen3_available() -> bool:
    """快速探测 MLX/qwen3 是否可用（导入失败/缺 libmlx.so → False）."""
    try:
        import qwen3_tts_cli  # noqa: F401

        import mlx.core  # noqa: F401

        return True
    except Exception:
        return False


def _load_model():
    """懒加载模型（线程安全，只加载一次）."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from qwen3_tts_cli.core import load_model

                _model = load_model(DEFAULT_MODEL_ID)
    return _model


async def _edge_synthesize(text: str) -> bytes:
    import edge_tts

    communicate = edge_tts.Communicate(text, voice="zh-CN-XiaoxiaoNeural")
    audio = b""
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio += chunk["data"]
    if not audio:
        raise RuntimeError("edge-tts 未返回音频")
    return audio


def _synthesize_wav(model, text: str, speaker: str, language: str, out_path: Path) -> None:
    """用已加载的模型合成 WAV（镜像 qwen3_tts_cli.core.synthesize 的核心逻辑）."""
    import mlx.core as mx
    import numpy as np
    from scipy.io import wavfile

    from qwen3_tts_cli.core import (
        get_supported_speakers,
        resolve_speaker,
        split_text_for_streaming,
    )

    supported = get_supported_speakers(model)
    if not supported:
        raise RuntimeError("模型未返回 speaker 列表")
    resolved = resolve_speaker(speaker, supported)

    mx.random.seed(42)
    gen_params = {
        "speaker": resolved,
        "language": language,
        "temperature": 0.2,
        "top_k": 20,
        "top_p": 0.85,
        "repetition_penalty": 1.1,
        "max_tokens": 1024,
        "stream": False,
        "instruct": "用自然、稳定、清晰的语气朗读，不要夸张演绎。",
    }

    parts = []
    for segment in split_text_for_streaming(text, 0):
        seg_params = dict(gen_params)
        seg_params["text"] = segment
        seg_parts = []
        for result in model.generate_custom_voice(**seg_params):
            seg_parts.append(result.audio)
        if not seg_parts:
            continue
        parts.append(seg_parts[0] if len(seg_parts) == 1 else mx.concatenate(seg_parts))

    if not parts:
        raise RuntimeError("未生成任何音频")
    full = mx.concatenate(parts)
    audio_np = np.array(full)
    wavfile.write(out_path, DEFAULT_SAMPLE_RATE, audio_np)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/tts")
def tts(req: TTSRequest):
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text 不能为空")

    engine = ENGINE
    # qwen3 引擎：MLX 不可用时回退
    if engine in ("auto", "qwen3"):
        if engine == "qwen3" or _qwen3_available():
            try:
                model = _load_model()
                with tempfile.TemporaryDirectory() as tmp:
                    out = Path(tmp) / "out.wav"
                    _synthesize_wav(model, text, req.speaker or DEFAULT_SPEAKER, req.language or DEFAULT_LANGUAGE, out)
                    data = out.read_bytes()
                return Response(content=data, media_type="audio/wav")
            except Exception as e:  # noqa: BLE001
                if engine == "qwen3":
                    raise HTTPException(status_code=500, detail=f"qwen3: {type(e).__name__}: {e}") from e
                print(f"[tts] qwen3 失败，回退 edge-tts: {e}", flush=True)

    # edge-tts 兜底
    try:
        audio = asyncio.run(_edge_synthesize(text))
        return Response(content=audio, media_type="audio/mpeg")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"edge: {type(e).__name__}: {e}") from e


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Qwen3-TTS local HTTP server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--engine", choices=("auto", "qwen3", "edge"), default="auto")
    args = parser.parse_args()

    import uvicorn

    ENGINE = args.engine
    uvicorn.run(app, host=args.host, port=args.port)
