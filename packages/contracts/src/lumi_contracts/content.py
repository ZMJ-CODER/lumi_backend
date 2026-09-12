"""多模态内容层（方案 §2.1 / §2.2）：``ContentPart`` + 处理器注册表。

两条契约：

* **内容载体四选一**（``data`` / ``artifact_ref`` / ``url`` / ``stream_ref``，互斥）：
  图片/音视频/大文档一律 ``artifact_ref``；**SSE 只传引用 + 元数据**（``to_event_ref()``），
  二进制永远不进事件流；
* **处理器必须声明完整契约**（不只 ``can_handle``）：输入模态/MIME/大小、输出模态与
  Schema、是否需要模型/本地文件、是否支持流式、是否允许降级、超时。处理链上每一步的
  输出契约必须能对接下一步的输入，否则**注册即报错**。

Pipeline 中间件只承担生命周期切面（:data:`PIPELINE_STAGES`），业务并行由 DAG 管理。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Modality(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    FILE = "file"


#: 必须走 ``artifact_ref`` 的模态（体积大、不能进 SSE）。
ARTIFACT_ONLY_MODALITIES: frozenset[str] = frozenset({Modality.IMAGE, Modality.AUDIO, Modality.VIDEO, Modality.FILE})

#: Pipeline 生命周期切面（顺序即契约）。
PIPELINE_STAGES: tuple[str, ...] = (
    "ingress", "normalize", "security_check", "capability_check", "process", "projection",
)

#: 内联文本上限（超过即必须转 artifact/url）。
INLINE_TEXT_MAX_BYTES = 65_536


class ContentPart(BaseModel):
    """一段内容（正文只在内联文本时出现）。"""

    model_config = ConfigDict(extra="ignore")

    part_id: str = ""
    modality: str = Modality.TEXT.value
    mime_type: str = "text/plain"
    representation: str = ""
    schema_version: int = 1
    # ── 载体四选一 ──
    data: str = ""
    artifact_ref: str = ""
    url: str = ""
    stream_ref: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate(self) -> "ContentPart":
        carriers = [name for name in ("data", "artifact_ref", "url", "stream_ref") if getattr(self, name)]
        if len(carriers) != 1:
            raise ValueError("ContentPart 的载体必须四选一（data / artifact_ref / url / stream_ref）")
        modality = str(self.modality or "").strip().casefold()
        if modality in ARTIFACT_ONLY_MODALITIES and carriers[0] == "data":
            raise ValueError(f"{modality} 必须用 artifact_ref/url/stream_ref 承载，不能内联进 data")
        if carriers[0] == "data" and len(self.data.encode("utf-8")) > INLINE_TEXT_MAX_BYTES:
            raise ValueError("内联内容超限：请改走 artifact_ref")
        return self

    @property
    def carrier(self) -> str:
        for name in ("data", "artifact_ref", "url", "stream_ref"):
            if getattr(self, name):
                return name
        return ""

    def to_event_ref(self) -> dict[str, Any]:
        """公开事件里的形态：**只有引用 + 元数据**，不含二进制/内联正文。"""
        ref = {
            "part_id": self.part_id,
            "modality": str(self.modality),
            "mime_type": self.mime_type,
            "schema_version": int(self.schema_version),
            "carrier": self.carrier,
        }
        if self.carrier == "artifact_ref":
            ref["artifact_id"] = self.artifact_ref
        elif self.carrier in {"url", "stream_ref"}:
            # URL 只报 host 之外的引用标识，不把可下载地址放进事件
            ref["ref"] = getattr(self, self.carrier)
        if self.carrier == "data":
            ref["inline"] = True
        return ref


class ProcessorCapability(BaseModel):
    """处理器的完整能力契约（注册即冻结）。"""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    input_modality: str = Modality.TEXT.value
    supported_mime_types: list[str] = Field(default_factory=list)
    max_input_size_bytes: int = INLINE_TEXT_MAX_BYTES
    output_modality: str = Modality.TEXT.value
    output_schema: str = ""
    requires_model: bool = False
    requires_local_file: bool = False
    supports_streaming: bool = False
    allows_degradation: bool = False
    timeout_seconds: int = 30

    def accepts(self, modality: str, mime_type: str, size_bytes: int = 0) -> bool:
        if str(modality or "").strip().casefold() != str(self.input_modality).strip().casefold():
            return False
        mime = str(mime_type or "").strip().casefold()
        if self.supported_mime_types and mime not in {item.casefold() for item in self.supported_mime_types}:
            return False
        return not size_bytes or int(size_bytes) <= int(self.max_input_size_bytes)


class ProcessorRegistry:
    """处理器注册表（按契约查找 + 处理链校验）。"""

    def __init__(self) -> None:
        self._processors: dict[str, ProcessorCapability] = {}

    def register(self, processor: ProcessorCapability) -> ProcessorCapability:
        name = str(processor.name or "").strip()
        if not name:
            raise ValueError("处理器必须声明 name")
        if not processor.supported_mime_types:
            raise ValueError(f"处理器 {name} 必须声明 supported_mime_types（不然只能靠猜）")
        self._processors[name] = processor
        return processor

    def get(self, name: str) -> ProcessorCapability | None:
        return self._processors.get(str(name or "").strip())

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._processors))

    def find(self, modality: str, mime_type: str, *, size_bytes: int = 0) -> tuple[ProcessorCapability, ...]:
        return tuple(
            item for item in self._processors.values() if item.accepts(modality, mime_type, size_bytes)
        )

    def validate_chain(self, names: list[str]) -> tuple[ProcessorCapability, ...]:
        """校验处理链：每一步的输出模态/Schema 必须能对接下一步。"""
        chain: list[ProcessorCapability] = []
        for raw in names or []:
            processor = self.get(raw)
            if processor is None:
                raise ValueError(f"处理链引用了未注册的处理器：{raw}")
            if chain:
                previous = chain[-1]
                if str(previous.output_modality) != str(processor.input_modality):
                    raise ValueError(
                        f"处理链断裂：{previous.name} 输出 {previous.output_modality}，"
                        f"{processor.name} 需要 {processor.input_modality}"
                    )
            chain.append(processor)
        return tuple(chain)


__all__ = [
    "ARTIFACT_ONLY_MODALITIES",
    "INLINE_TEXT_MAX_BYTES",
    "PIPELINE_STAGES",
    "ContentPart",
    "Modality",
    "ProcessorCapability",
    "ProcessorRegistry",
]
