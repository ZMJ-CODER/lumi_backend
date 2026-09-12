"""产物引用与来源引用。

与旧契约（``app.agents.skills.output_contract``）保持**字段级一致**，方便第一阶段
双向重导出；新增 ``schema_name`` 让产物也能被投影按契约识别。

保留类别（retention classes）与"请求值 → 生效值"的解析也定义在这里：事件、快照、
API 元数据三处产物引用共用同一套字段，避免各自推导一份 TTL（那样必然出现"ref 写
30 天、文件 7 天被删"的静默夹取）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel


class RetentionClass(StrEnum):
    """产物保留类别：决定"谁来清、按什么策略清"。

    * ``EPHEMERAL_ARCHIVE``：临时归档（运行中任务的过程日志溢出），保留期最短，
      由**归档清理器**删除；
    * ``USER_ARTIFACT``：用户产物（任务交付物 / 用户另存），按**工作区存储策略**，
      归档清理器绝不碰它；
    * ``AUDIT_ARCHIVE``：审计归档（已完成任务的完整过程日志），中等保留期，
      由**归档清理器**删除。
    """

    EPHEMERAL_ARCHIVE = "EPHEMERAL_ARCHIVE"
    USER_ARTIFACT = "USER_ARTIFACT"
    AUDIT_ARCHIVE = "AUDIT_ARCHIVE"


#: 未标注类别的产物一律按"用户产物"处理（fail-safe：宁可多留，不可误删审计/交付物）。
DEFAULT_RETENTION_CLASS = RetentionClass.USER_ARTIFACT

#: 合法类别集合（外部输入归一用）。
RETENTION_CLASSES: frozenset[str] = frozenset(item.value for item in RetentionClass)

#: 由归档清理器负责的类别（用户产物不在其中）。
ARCHIVE_RETENTION_CLASSES: frozenset[str] = frozenset(
    {RetentionClass.EPHEMERAL_ARCHIVE.value, RetentionClass.AUDIT_ARCHIVE.value}
)

#: 每一条产物引用都必须携带的保留策略字段（契约级要求；缺一个就无法回答
#: "这条产物请求活多久、实际活多久、依据什么、被谁夹到多久"）。
ARTIFACT_RETENTION_FIELDS: tuple[str, ...] = (
    "retention_class",
    "requested_expires_at",
    "effective_expires_at",
    "retention_policy_source",
    "retention_clamp_reason",
)


def retention_class_of(value: object, *, default: str = "") -> str:
    """归一保留类别（大小写不敏感）；未知/缺失退回 ``default``。

    ``default=""`` 表示"没有类别信息"（例如旧版签名 artifact_id），由调用方按文件名
    兜底分类；显式传 ``DEFAULT_RETENTION_CLASS`` 则把未知值也收敛到用户产物。
    """
    text = str(getattr(value, "value", value) or "").strip().upper()
    if text in RETENTION_CLASSES:
        return text
    if default:
        return retention_class_of(default, default="") or DEFAULT_RETENTION_CLASS.value
    return ""


def iso_utc(epoch: float) -> str:
    """epoch 秒 → UTC ISO-8601 字符串（秒级精度，前端 ``Date`` 可直接解析）。

    统一截到秒：产物引用的签发时间是整数秒（签名字段），若这里保留微秒，
    "写盘时算出的到期时间"与"从引用反解出的到期时间"会差几微秒、无法直接对拍。
    """
    moment = datetime.fromtimestamp(max(0.0, float(epoch)), tz=timezone.utc).replace(microsecond=0)
    return moment.isoformat()


def parse_iso_utc(text: object) -> float:
    """UTC ISO-8601 → epoch 秒；解析失败返回 ``0.0``（不抛错）。"""
    raw = str(text or "").strip()
    if not raw:
        return 0.0
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


@dataclass(frozen=True, slots=True)
class RetentionDecision:
    """一次保留决策：请求值、生效值、策略来源与（必要时）夹取原因。"""

    retention_class: str
    requested_seconds: int
    effective_seconds: int
    requested_expires_at: str
    effective_expires_at: str
    retention_policy_source: str
    #: 夹取原因（未夹取时为空串）；**绝不静默夹取**：原因必须能进 ref 元数据与日志。
    retention_clamp_reason: str = ""
    #: 施加天花板的那一项（设置名/常量名），未夹取时为空串。
    clamp_source: str = ""

    @property
    def clamped(self) -> bool:
        return self.effective_seconds < self.requested_seconds

    def as_metadata(self) -> dict[str, object]:
        """接入产物引用/API 元数据的公共字段（字段恒定存在，未夹取时原因为空串）。"""
        values: dict[str, object] = {
            "retention_class": self.retention_class,
            "requested_expires_at": self.requested_expires_at,
            "effective_expires_at": self.effective_expires_at,
            "retention_policy_source": self.retention_policy_source,
            "retention_clamp_reason": self.retention_clamp_reason,
            "retention_clamped": self.clamped,
        }
        assert set(ARTIFACT_RETENTION_FIELDS) <= set(values)  # 契约字段不得缺失
        return values


def resolve_retention(
    *,
    retention_class: str,
    requested_seconds: int,
    requested_at: float,
    policy_source: str,
    ceiling_seconds: int | None = None,
    ceiling_source: str = "",
) -> RetentionDecision:
    """请求保留期 → 生效保留期，并把夹取写成可审计的原因。

    ``ceiling_seconds`` 是本类别的硬上限（存储/读取契约或工作区策略）。当
    ``requested > ceiling`` 时生效值取上限，且 ``retention_clamp_reason`` 必须写明
    "请求多少、被谁夹到多少"——这就是"绝不静默夹取"的落点。
    """
    cls = retention_class_of(retention_class, default=DEFAULT_RETENTION_CLASS)
    requested = max(1, int(requested_seconds or 1))
    ceiling = None if ceiling_seconds is None else max(1, int(ceiling_seconds))
    effective = requested if ceiling is None else min(requested, ceiling)
    reason = ""
    source = ""
    if effective < requested:
        source = str(ceiling_source or "unspecified_ceiling")
        reason = (
            f"requested {requested}s from {str(policy_source or 'unspecified')} "
            f"exceeds ceiling {ceiling}s from {source}; effective {effective}s"
        )
    return RetentionDecision(
        retention_class=cls,
        requested_seconds=requested,
        effective_seconds=effective,
        requested_expires_at=iso_utc(requested_at + requested),
        effective_expires_at=iso_utc(requested_at + effective),
        retention_policy_source=str(policy_source or ""),
        retention_clamp_reason=reason,
        clamp_source=source,
    )


class ArtifactRef(BaseModel):
    """后端产物引用；不允许把宿主路径或凭据暴露给模型。"""

    ref_id: str
    name: str = ""
    media_type: str = "application/octet-stream"
    size: int | None = None
    # 契约标识（可选）：形如 lumi.workspace_navigator.result@1
    schema_name: str = ""
    # 仅服务端可见的定位信息；投影阶段必须剥离。
    internal_locator: str = ""
    # ── 保留策略（每一条产物引用都必须能回答"谁、依据什么、什么时候删它"）──
    #: ``RetentionClass`` 之一（``EPHEMERAL_ARCHIVE`` / ``USER_ARTIFACT`` / ``AUDIT_ARCHIVE``）。
    retention_class: str = ""
    #: 请求到期时间（策略请求值，UTC ISO-8601）。
    requested_expires_at: str = ""
    #: 实际生效到期时间（= 请求值与天花板取较小者）。
    effective_expires_at: str = ""
    #: 策略来源（settings 字段名，例如 ``LOG_ARCHIVE_RETENTION_COMPLETED_JOB_SECONDS``）。
    retention_policy_source: str = ""
    #: 被夹取时的原因（未夹取为空串；**不允许静默夹取**）。
    retention_clamp_reason: str = ""


class Citation(BaseModel):
    """可展示的来源定位信息，正文只保留短摘录。"""

    title: str = ""
    source: str = ""
    snippet: str = ""
    locator: str = ""


def artifact_refs_from(value: object) -> list[ArtifactRef]:
    """把遗留的产物引用（dict / 对象）归一为 ``ArtifactRef``。"""
    items = value if isinstance(value, (list, tuple)) else []
    out: list[ArtifactRef] = []
    for item in items:
        if isinstance(item, ArtifactRef):
            out.append(item)
            continue
        if isinstance(item, dict):
            payload = {key: item[key] for key in item if key in ArtifactRef.model_fields}
            if payload.get("ref_id"):
                out.append(ArtifactRef.model_validate(payload))
            continue
        ref_id = str(getattr(item, "ref_id", "") or "")
        if ref_id:
            out.append(ArtifactRef(
                ref_id=ref_id,
                name=str(getattr(item, "name", "") or ""),
                media_type=str(getattr(item, "media_type", "") or "application/octet-stream"),
                size=getattr(item, "size", None),
                retention_class=retention_class_of(getattr(item, "retention_class", None), default=""),
                requested_expires_at=str(getattr(item, "requested_expires_at", "") or ""),
                effective_expires_at=str(getattr(item, "effective_expires_at", "") or ""),
                retention_policy_source=str(getattr(item, "retention_policy_source", "") or ""),
                retention_clamp_reason=str(getattr(item, "retention_clamp_reason", "") or ""),
            ))
    return out


__all__ = [
    "ARCHIVE_RETENTION_CLASSES",
    "ARTIFACT_RETENTION_FIELDS",
    "DEFAULT_RETENTION_CLASS",
    "RETENTION_CLASSES",
    "ArtifactRef",
    "Citation",
    "RetentionClass",
    "RetentionDecision",
    "artifact_refs_from",
    "iso_utc",
    "parse_iso_utc",
    "resolve_retention",
    "retention_class_of",
]
