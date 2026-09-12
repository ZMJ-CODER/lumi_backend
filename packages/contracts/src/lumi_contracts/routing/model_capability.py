"""模型能力画像与**标准化降级决策表**（方案 §2.3 / §3 能力协商）。

两个契约：

* :class:`ModelCapabilityProfile`：描述"这个模型能吃什么、能吐什么、上限多少"，
  并提供与既有档位能力旗标（``supports_vision`` / ``max_context`` …）的适配器——
  不新造第二套能力来源；
* :func:`decide_degradation`：路由**不能只判断 supports("image")**。失配时按固定决策表
  给出标准动作，且两条铁律写死在返回结构里：

  1. **信息有损的降级必须让用户知道** → ``lossy=True`` 时 ``process_notice`` 必非空；
  2. **能力缺失的降级必须阻断而不是编造** → 需要工具但模型不支持工具时返回 ``BLOCK``
     （绝不静默降级成"纯文本瞎答"）。

顺序（先挑无损/无损感知的出路，再考虑有损，最后才阻断）：

===============  ==========================================  ==========================
失配             标准动作                                    用户感知
===============  ==========================================  ==========================
不支持该模态      有候选 → ``AUTO_SWITCH``；无候选 → ``BLOCK``   自动切换或明确缺什么
支持但超限        ``PRECOMPRESS``（压缩/降分辨率/取前 N 张）    无感或轻提示
不支持视频        ``EXTRACT_FRAMES``（抽帧 + 告知不含音频）      轻提示（信息有损）
不支持流式        ``SIMULATE_STREAM``（非流式拿全量后一次性发）  无感（首字延迟长）
不支持工具        预检失败 ``BLOCK``（禁止纯文本瞎答）          明确阻断
===============  ==========================================  ==========================
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lumi_contracts.events.errors import UnifiedError, translate_error


class DegradationAction(StrEnum):
    """标准降级动作（前端只需按这几种分派）。"""

    NONE = "NONE"
    AUTO_SWITCH = "AUTO_SWITCH"
    PRECOMPRESS = "PRECOMPRESS"
    EXTRACT_FRAMES = "EXTRACT_FRAMES"
    SIMULATE_STREAM = "SIMULATE_STREAM"
    BLOCK = "BLOCK"


#: 默认图像/视频上限（模型未声明时的保守值）。
DEFAULT_MAX_IMAGE_COUNT = 4
DEFAULT_MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_VIDEO_SECONDS = 30

#: 抽帧默认帧数（信息有损，必须在 process 事件里说明）。
DEFAULT_FRAME_COUNT = 8


class ModelCapabilityProfile(BaseModel):
    """模型能力画像（路由/预检的唯一输入）。"""

    model_config = ConfigDict(extra="ignore")

    model: str = ""
    input_modalities: list[str] = Field(default_factory=lambda: ["text"])
    output_modalities: list[str] = Field(default_factory=lambda: ["text"])
    supports_streaming: bool = True
    supports_tools: bool = False
    supports_parallel_tools: bool = False
    supports_json: bool = False
    max_context_tokens: int = 32_000
    max_image_count: int = DEFAULT_MAX_IMAGE_COUNT
    max_image_size_bytes: int = DEFAULT_MAX_IMAGE_SIZE_BYTES
    max_video_duration_seconds: int = DEFAULT_MAX_VIDEO_SECONDS
    image_input_type: str = "url"  # url | base64 | artifact_ref
    supports_byok: bool = True

    def accepts(self, modality: str) -> bool:
        return str(modality or "").strip().casefold() in {
            item.casefold() for item in self.input_modalities
        }


def from_role_capabilities(capabilities: Mapping[str, Any] | None, *, model: str = "") -> ModelCapabilityProfile:
    """既有档位能力旗标 → 能力画像（**唯一适配点**，不新增第二套能力来源）。"""
    caps = dict(capabilities or {})
    modalities = ["text"]
    if caps.get("supports_vision"):
        modalities.append("image")
    if caps.get("supports_audio"):
        modalities.append("audio")
    if caps.get("supports_video"):
        modalities.append("video")
    return ModelCapabilityProfile(
        model=str(model or ""),
        input_modalities=modalities,
        output_modalities=["text"],
        supports_streaming=bool(caps.get("supports_streaming", True)),
        supports_tools=bool(caps.get("supports_tools", False)),
        supports_parallel_tools=bool(caps.get("supports_parallel_tools", False)),
        supports_json=bool(caps.get("supports_json", False)),
        max_context_tokens=int(caps.get("max_context") or caps.get("max_context_tokens") or 32_000),
        max_image_count=int(caps.get("max_image_count") or DEFAULT_MAX_IMAGE_COUNT),
        max_image_size_bytes=int(caps.get("max_image_size_bytes") or DEFAULT_MAX_IMAGE_SIZE_BYTES),
        max_video_duration_seconds=int(caps.get("max_video_duration_seconds") or DEFAULT_MAX_VIDEO_SECONDS),
        image_input_type=str(caps.get("image_input_type") or "url"),
        supports_byok=bool(caps.get("supports_byok", True)),
    )


@dataclass(frozen=True)
class ModalityRequest:
    """任务对模型的需求（来自 TaskProfile / ContentPart 列表）。"""

    modalities: tuple[str, ...] = ("text",)
    image_count: int = 0
    max_image_size_bytes: int = 0
    video_duration_seconds: int = 0
    needs_tools: bool = False
    needs_streaming: bool = False
    target: str = ""

    @property
    def wants_video(self) -> bool:
        return "video" in {item.casefold() for item in self.modalities}


@dataclass(frozen=True)
class DegradationDecision:
    """降级决策（标准化执行 + 用户可见性 + 阻断语义）。"""

    action: str = DegradationAction.NONE.value
    reason_code: str = ""
    target_model: str = ""
    target_profile: ModelCapabilityProfile | None = None
    #: 是否信息有损（有损必须给 process_notice，见 __post_init__）
    lossy: bool = False
    process_notice: str = ""
    dropped_modalities: tuple[str, ...] = ()
    error: UnifiedError | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.lossy and not self.process_notice:
            raise ValueError("信息有损的降级必须给出 process_notice（方案 §2.3 铁律）")
        if self.action == DegradationAction.BLOCK.value and self.error is None:
            raise ValueError("BLOCK 必须携带 UnifiedError")

    @property
    def blocked(self) -> bool:
        return self.action == DegradationAction.BLOCK.value

    @property
    def lossless(self) -> bool:
        return not self.lossy

    def as_process_event(self) -> dict[str, Any]:
        """可直接当 ``process`` 事件载荷（strip_unsafe_payload 白名单内的字段）。"""
        return {
            "kind": "thinking",
            "title": "能力适配",
            "summary": self.process_notice,
            "status": "completed" if not self.blocked else "failed",
            "detail": self.reason_code,
        }


def _block(code: str, *, reason: str, details: dict[str, Any] | None = None) -> DegradationDecision:
    return DegradationDecision(
        action=DegradationAction.BLOCK.value,
        reason_code=reason,
        error=translate_error({"code": code}),
        details=dict(details or {}),
    )


def decide_degradation(
    profile: ModelCapabilityProfile,
    request: ModalityRequest,
    *,
    candidates: Sequence[ModelCapabilityProfile] | None = None,
    frame_count: int = DEFAULT_FRAME_COUNT,
) -> DegradationDecision:
    """按方案 §2.3 的决策表给出标准动作（顺序即优先级）。"""
    # ① 工具能力缺失：**阻断**，绝不静默降级成纯文本回答。
    if request.needs_tools and not profile.supports_tools:
        return _block(
            "CAPABILITY_UNAVAILABLE",
            reason="MODEL_TOOLS_UNSUPPORTED",
            details={"required": "tools", "model": profile.model},
        )

    wanted = {str(item).strip().casefold() for item in request.modalities if str(item).strip()}
    unsupported = sorted(item for item in wanted if item not in {"text"} and not profile.accepts(item))

    # ② 视频不支持但支持图片：先走**转换链**（抽帧），而不是直接判"模态缺失"。
    #    信息有损 → 必须在 process 事件里说明"不含音频"。
    remaining = list(unsupported)
    if "video" in remaining and profile.accepts("image"):
        remaining = [item for item in remaining if item != "video"]
        if not remaining:
            return DegradationDecision(
                action=DegradationAction.EXTRACT_FRAMES.value,
                reason_code="VIDEO_TO_FRAMES",
                lossy=True,
                process_notice=f"当前模型不支持视频，已抽取 {frame_count} 帧图像分析（不含音频）。",
                dropped_modalities=("video",),
                details={"frames": int(frame_count), "audio_included": False},
            )

    # ③ 仍然缺模态：先在同会话候选里自动切换；没有候选才阻断。
    if remaining:
        for candidate in candidates or ():
            if all(candidate.accepts(item) for item in remaining):
                return DegradationDecision(
                    action=DegradationAction.AUTO_SWITCH.value,
                    reason_code="MODEL_MODALITY_SWITCH",
                    target_model=candidate.model,
                    target_profile=candidate,
                    process_notice=(
                        f"当前模型不支持 {'/'.join(remaining)}，已自动切换到 {candidate.model}。"
                    ),
                    details={"unsupported": remaining, "switched_from": profile.model},
                )
        return _block(
            "CAPABILITY_UNAVAILABLE",
            reason="MODEL_MODALITY_UNSUPPORTED",
            details={"unsupported": remaining, "model": profile.model},
        )

    # ④ 支持但超限：降级预处理（压缩 / 降分辨率 / 只取前 N 张）
    if "image" in wanted and profile.accepts("image"):
        if request.image_count > profile.max_image_count:
            return DegradationDecision(
                action=DegradationAction.PRECOMPRESS.value,
                reason_code="IMAGE_COUNT_OVER_LIMIT",
                lossy=True,
                process_notice=(
                    f"图片数量超过当前模型上限（{request.image_count} > {profile.max_image_count}），"
                    f"已按顺序只取前 {profile.max_image_count} 张。"
                ),
                dropped_modalities=("image",),
                details={"kept": profile.max_image_count, "received": request.image_count},
            )
        if request.max_image_size_bytes and request.max_image_size_bytes > profile.max_image_size_bytes:
            return DegradationDecision(
                action=DegradationAction.PRECOMPRESS.value,
                reason_code="IMAGE_SIZE_OVER_LIMIT",
                lossy=True,
                process_notice="图片超过模型单张上限，已压缩分辨率后提交。",
                dropped_modalities=(),
                details={
                    "limit_bytes": profile.max_image_size_bytes,
                    "received_bytes": request.max_image_size_bytes,
                },
            )

    # ⑤ 不支持流式：拿全量后模拟成增量发出（协议不变，用户只感到首字变慢）
    if request.needs_streaming and not profile.supports_streaming:
        return DegradationDecision(
            action=DegradationAction.SIMULATE_STREAM.value,
            reason_code="STREAMING_SIMULATED",
            process_notice="",
            details={"buffer_until_complete": True},
        )

    return DegradationDecision(action=DegradationAction.NONE.value, reason_code="MODEL_CAPABILITY_MATCH")


__all__ = [
    "DEFAULT_FRAME_COUNT",
    "DEFAULT_MAX_IMAGE_COUNT",
    "DEFAULT_MAX_IMAGE_SIZE_BYTES",
    "DEFAULT_MAX_VIDEO_SECONDS",
    "DegradationAction",
    "DegradationDecision",
    "ModelCapabilityProfile",
    "ModalityRequest",
    "decide_degradation",
    "from_role_capabilities",
]
