# -*- coding: utf-8 -*-
"""按需 TTS 链路验证（验证后删除）."""
import asyncio

from app.services.speech import detect_audio_meta, synthesize_speech


async def main() -> None:
    audio = await synthesize_speech("你好，这是一次语音合成测试。")
    assert audio, "TTS 返回为空"
    ext, mime = detect_audio_meta(audio)
    print("TTS OK: bytes=", len(audio), "| ext=", ext, "| mime=", mime)


if __name__ == "__main__":
    asyncio.run(main())
