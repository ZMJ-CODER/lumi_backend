"""模型能力降级决策表回归（方案 §2.3）。

两条铁律必须有机器可验证的证据：

1. **信息有损必须告知**：``lossy=True`` 而 ``process_notice`` 为空 → 构造即报错；
2. **能力缺失必须阻断**：需要工具但模型不支持工具 → ``BLOCK``（不是纯文本瞎答）。
"""

from __future__ import annotations

import pytest

from lumi_contracts import (
    DegradationAction,
    DegradationDecision,
    ModelCapabilityProfile,
    ModalityRequest,
    decide_degradation,
    from_role_capabilities,
)

TEXT_MODEL = ModelCapabilityProfile(model="deepseek-chat", supports_tools=True, supports_streaming=True)
VISION_MODEL = ModelCapabilityProfile(
    model="gpt-4o",
    input_modalities=["text", "image"],
    supports_tools=True,
    max_image_count=4,
    max_image_size_bytes=5 * 1024 * 1024,
)


# ── 适配器：不新造第二套能力来源 ─────────────────────────


def test_role_capabilities_adapter_maps_existing_flags():
    profile = from_role_capabilities(
        {"supports_vision": True, "supports_tools": True, "max_context": 128_000}, model="vlm"
    )
    assert profile.accepts("image") and profile.accepts("text")
    assert not profile.accepts("video")
    assert profile.supports_tools is True
    assert profile.max_context_tokens == 128_000
    plain = from_role_capabilities({"supports_vision": False})
    assert not plain.accepts("image")


# ── 决策表：③④⑤ 无损/有损降级 ───────────────────────────


def test_matching_capability_needs_no_degradation():
    decision = decide_degradation(TEXT_MODEL, ModalityRequest(modalities=("text",)))
    assert decision.action == DegradationAction.NONE.value
    assert decision.lossless and not decision.blocked


def test_image_over_count_is_precompressed_with_notice():
    decision = decide_degradation(
        VISION_MODEL, ModalityRequest(modalities=("text", "image"), image_count=9)
    )
    assert decision.action == DegradationAction.PRECOMPRESS.value
    assert decision.reason_code == "IMAGE_COUNT_OVER_LIMIT"
    assert decision.lossy and decision.process_notice, "有损必须给用户可见说明"
    assert decision.details["kept"] == 4 and decision.details["received"] == 9
    # 可作为 process 事件载荷直接发出
    assert decision.as_process_event()["summary"] == decision.process_notice


def test_image_over_size_is_precompressed():
    decision = decide_degradation(
        VISION_MODEL,
        ModalityRequest(modalities=("text", "image"), image_count=1, max_image_size_bytes=20 * 1024 * 1024),
    )
    assert decision.action == DegradationAction.PRECOMPRESS.value
    assert decision.reason_code == "IMAGE_SIZE_OVER_LIMIT"
    assert decision.lossy and decision.process_notice


def test_video_falls_back_to_frames_and_declares_audio_loss():
    decision = decide_degradation(
        VISION_MODEL, ModalityRequest(modalities=("text", "video"), video_duration_seconds=12)
    )
    assert decision.action == DegradationAction.EXTRACT_FRAMES.value
    assert decision.lossy
    assert "不含音频" in decision.process_notice, "信息有损必须说清楚丢了什么"
    assert decision.dropped_modalities == ("video",)
    assert decision.details["audio_included"] is False


def test_no_streaming_simulates_delta_without_loss():
    profile = ModelCapabilityProfile(model="slow", supports_streaming=False, supports_tools=True)
    decision = decide_degradation(profile, ModalityRequest(needs_streaming=True))
    assert decision.action == DegradationAction.SIMULATE_STREAM.value
    assert decision.lossless, "协议不变，不算信息有损"
    assert decision.details["buffer_until_complete"] is True


# ── 决策表：①② 自动切换 / 阻断 ──────────────────────────


def test_unsupported_modality_switches_to_a_capable_candidate():
    decision = decide_degradation(
        TEXT_MODEL, ModalityRequest(modalities=("text", "image"), image_count=1), candidates=[VISION_MODEL]
    )
    assert decision.action == DegradationAction.AUTO_SWITCH.value
    assert decision.target_model == "gpt-4o"
    assert decision.target_profile is VISION_MODEL
    # 换模型不是"信息有损"，但必须留 process 说明（用户要知道换了）
    assert decision.lossless and decision.process_notice
    assert "自动切换" in decision.process_notice


def test_unsupported_modality_without_candidate_is_blocked():
    decision = decide_degradation(TEXT_MODEL, ModalityRequest(modalities=("text", "image")))
    assert decision.action == DegradationAction.BLOCK.value
    assert decision.blocked and decision.error is not None
    assert decision.error.code == "CAPABILITY_UNAVAILABLE"
    assert decision.error.safe_message
    assert decision.details["unsupported"] == ["image"]


def test_candidate_must_cover_every_unsupported_modality():
    audio_only = ModelCapabilityProfile(model="whisper", input_modalities=["text", "audio"])
    decision = decide_degradation(
        TEXT_MODEL, ModalityRequest(modalities=("text", "image")), candidates=[audio_only]
    )
    assert decision.blocked, "候选能力不足时不得切过去"


def test_model_without_tools_is_blocked_never_silently_answered():
    profile = ModelCapabilityProfile(model="no-tools", supports_tools=False)
    decision = decide_degradation(
        profile, ModalityRequest(modalities=("text",), needs_tools=True, target="write file")
    )
    assert decision.blocked
    assert decision.reason_code == "MODEL_TOOLS_UNSUPPORTED"
    assert decision.error is not None and decision.error.code == "CAPABILITY_UNAVAILABLE"
    # 阻断优先于其它降级：即使候选模型可用也不偷偷切换执行
    assert decision.action == DegradationAction.BLOCK.value


def test_tool_block_wins_over_modality_switch():
    """工具缺失是"不能编造"的硬阻断，必须先于自动切换判断。"""
    decision = decide_degradation(
        ModelCapabilityProfile(model="no-tools"),
        ModalityRequest(modalities=("text", "image"), needs_tools=True),
        candidates=[VISION_MODEL],
    )
    assert decision.blocked and decision.reason_code == "MODEL_TOOLS_UNSUPPORTED"


# ── 铁律：结构上不允许"有损但不告知" ─────────────────────


def test_lossy_decision_without_notice_is_rejected_by_contract():
    with pytest.raises(ValueError, match="process_notice"):
        DegradationDecision(action=DegradationAction.PRECOMPRESS.value, lossy=True, process_notice="")


def test_block_decision_without_error_is_rejected_by_contract():
    with pytest.raises(ValueError, match="UnifiedError"):
        DegradationDecision(action=DegradationAction.BLOCK.value)


def test_every_action_has_a_reason_code():
    cases = [
        (TEXT_MODEL, ModalityRequest()),
        (VISION_MODEL, ModalityRequest(modalities=("text", "image"), image_count=9)),
        (VISION_MODEL, ModalityRequest(modalities=("text", "video"))),
        (TEXT_MODEL, ModalityRequest(modalities=("text", "image"))),
    ]
    for profile, request in cases:
        decision = decide_degradation(profile, request)
        assert decision.reason_code, f"{profile.model} 缺少稳定 reason_code"
        assert decision.reason_code.isupper() or decision.reason_code == "MODEL_CAPABILITY_MATCH"
