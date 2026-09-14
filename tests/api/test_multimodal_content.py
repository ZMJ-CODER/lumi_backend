"""多模态内容层回归（方案 §2.1 / §2.2）：载体互斥、引用化、注册表与处理链。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lumi_contracts.content import (
    ARTIFACT_ONLY_MODALITIES,
    PIPELINE_STAGES,
    ContentPart,
    Modality,
    ProcessorCapability,
    ProcessorRegistry,
)


# ── ContentPart：载体四选一 + 大对象必须引用 ──────────────


def test_carrier_must_be_exactly_one():
    with pytest.raises(ValidationError, match="四选一"):
        ContentPart(modality="text", data="hi", url="https://x")
    with pytest.raises(ValidationError, match="四选一"):
        ContentPart(modality="text")


def test_text_can_be_inline_and_is_bounded():
    part = ContentPart(part_id="p1", modality="text", data="你好")
    assert part.carrier == "data"
    ref = part.to_event_ref()
    assert ref["inline"] is True and "data" not in ref, "事件里不放内联正文"
    with pytest.raises(ValidationError, match="内联内容超限"):
        ContentPart(modality="text", data="x" * (65_536 + 1))


def test_large_media_must_use_a_reference():
    for modality in sorted(ARTIFACT_ONLY_MODALITIES):
        with pytest.raises(ValidationError, match="不能内联"):
            ContentPart(modality=modality, data="binary")
    image = ContentPart(
        part_id="p2", modality="image", mime_type="image/png", artifact_ref="artifact:img-1",
        metadata={"width": 1024, "height": 768, "thumbnail_ref": "artifact:thumb-1"},
    )
    ref = image.to_event_ref()
    assert ref["artifact_id"] == "artifact:img-1" and ref["modality"] == "image"
    assert "data" not in ref and "url" not in ref, "SSE 只传引用 + 元数据"
    assert ref["carrier"] == "artifact_ref"


def test_stream_and_url_carriers_reference_only():
    video = ContentPart(modality="video", mime_type="video/mp4", stream_ref="stream:v1")
    ref = video.to_event_ref()
    assert ref["carrier"] == "stream_ref" and ref["ref"] == "stream:v1"


def test_pipeline_stages_are_frozen():
    assert PIPELINE_STAGES == (
        "ingress", "normalize", "security_check", "capability_check", "process", "projection",
    )


# ── ProcessorRegistry：契约式注册 + 处理链校验 ───────────


def _registry() -> ProcessorRegistry:
    registry = ProcessorRegistry()
    registry.register(ProcessorCapability(
        name="ocr", input_modality=Modality.IMAGE.value, output_modality=Modality.TEXT.value,
        supported_mime_types=["image/png", "image/jpeg"], output_schema="ocr.text@1",
        max_input_size_bytes=10_000, requires_local_file=True, timeout_seconds=20,
    ))
    registry.register(ProcessorCapability(
        name="summarize", input_modality=Modality.TEXT.value, output_modality=Modality.TEXT.value,
        supported_mime_types=["text/plain"], output_schema="summary@1", requires_model=True,
    ))
    return registry


def test_registration_requires_explicit_mime_contract():
    registry = ProcessorRegistry()
    with pytest.raises(ValueError, match="supported_mime_types"):
        registry.register(ProcessorCapability(name="guessing"))
    with pytest.raises(ValueError, match="name"):
        registry.register(ProcessorCapability(name="", supported_mime_types=["text/plain"]))


def test_find_matches_modality_mime_and_size():
    registry = _registry()
    assert [item.name for item in registry.find("image", "image/png", size_bytes=1000)] == ["ocr"]
    assert registry.find("image", "image/png", size_bytes=99_999) == (), "超限不再匹配"
    assert registry.find("image", "application/pdf") == (), "未知 MIME 不猜处理器"
    assert registry.find("audio", "audio/wav") == ()


def test_chain_validation_rejects_broken_links_and_unknown_names():
    registry = _registry()
    assert [item.name for item in registry.validate_chain(["ocr", "summarize"])] == ["ocr", "summarize"]
    with pytest.raises(ValueError, match="未注册的处理器"):
        registry.validate_chain(["ocr", "ghost"])
    registry.register(ProcessorCapability(
        name="frames", input_modality=Modality.VIDEO.value, output_modality=Modality.AUDIO.value,
        supported_mime_types=["video/mp4"],
    ))
    with pytest.raises(ValueError, match="处理链断裂"):
        registry.validate_chain(["frames", "summarize"])


def test_registry_is_append_only_by_name():
    registry = _registry()
    assert registry.names() == ("ocr", "summarize")
    assert registry.get("ocr") is not None and registry.get("nope") is None
