"""统一结果引用体系（``ResultRef`` / ``ResultStore`` 的**决策面**）。

方案《结果存储、检查点与恢复（整合版）》第一节。本模块**不碰任何存储后端**：

* 定义统一引用结构 ``ResultRef``（含 ``schema_version`` / ``sha256`` / ``expires_at``）；
* 定义分层存储**决策**（多小的结果进 Redis、多大的落 Blob）；
* 定义按预算加载的**预算与裁剪规则**（只读摘要 / 只读字段 / 分页 / 字符预算）；
* 定义引用不可用的**稳定错误**（``RESULT_REF_EXPIRED`` /
  ``RESULT_REF_SCHEMA_MISMATCH`` — 绝不静默当成空结果）。

真正的读写由宿主实现（:class:`ResultBlobPort` / :class:`ResultKvPort`）；契约包保持
backend-neutral，不依赖 Redis / S3 / ``app``。

三条关键规则（方案的硬要求）：

1. **业务层只用引用**：不得依赖 Redis Key、绝对路径或 OSS 地址；
2. **引用带存储时的 ``schema_version``**：读取时按**存储时**的版本解析，
   永不用当前版本解析历史数据；解析失败降级为原始 Artifact 展示 +
   ``validation.schema_mismatch``；
3. **过期是明确错误**：返回 ``RESULT_REF_EXPIRED``，不静默当作空结果。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from lumi_contracts.common.errors import ContractError, ContractErrorCode
from lumi_contracts.execution.artifacts import ArtifactRef, iso_utc, parse_iso_utc

# ── 统一错误码（跨模块契约；不扩冻结 12 码表，按域登记）──────────
#: 引用已过期（时间到）：明确失败，不静默当空。
RESULT_REF_EXPIRED = "RESULT_REF_EXPIRED"
#: 引用指向的存储记录不存在/被清掉（与"过期"区分：过期有时间依据）。
RESULT_REF_UNAVAILABLE = "RESULT_REF_UNAVAILABLE"
#: ``sha256`` 与正文不符：完整性校验失败，拒绝交付（不是"读不到"）。
RESULT_REF_INTEGRITY_FAILED = "RESULT_REF_INTEGRITY_FAILED"
#: 存储时的 ``schema_version`` 与当前解析器不兼容：降级展示原始文本。
RESULT_REF_SCHEMA_MISMATCH = "validation.schema_mismatch"
#: 调用方没有该引用的所有权（跨用户/跨任务读取）。
RESULT_REF_FORBIDDEN = "RESULT_REF_FORBIDDEN"


class ResultStorageKind(StrEnum):
    """结果的分层存储位置（业务层只见引用，不见具体后端）。"""

    REDIS = "redis"
    LOCAL = "local"
    BLOB = "blob"


class ResultDegradation(StrEnum):
    """读取降级原因（读到"能展示的东西"但**不是**完整结构化结果）。"""

    NONE = "none"
    #: ``schema_version`` 不兼容：降级为原始 Artifact / 文本展示。
    SCHEMA_MISMATCH = "schema_mismatch"
    #: 完整性校验失败：正文不可信，只允许展示摘要引用。
    INTEGRITY_FAILED = "integrity_failed"
    #: 历史记录缺少结构化正文（旧版本只存了文本）。
    BODY_UNAVAILABLE = "body_unavailable"


#: ``ResultRef`` 的固定键（写入方与读取方共用；测试据此断言形状不漂移）。
RESULT_REF_FIELDS: tuple[str, ...] = (
    "id",
    "sha256",
    "storage_kind",
    "content_type",
    "size",
    "owner_id",
    "job_id",
    "step_id",
    "schema_name",
    "schema_version",
    "expires_at",
    "created_at",
)


class ResultRefError(ContractError):
    """引用不可用（统一错误码 + 可审计细节）；绝不静默返回空结果。"""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        retryable: bool = False,
        suggested_action: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code,
            message or _DEFAULT_MESSAGES.get(str(code), "结果引用不可用"),
            retryable=retryable,
            suggested_action=suggested_action or _DEFAULT_ACTIONS.get(str(code), ""),
            details=details,
        )


_DEFAULT_MESSAGES: dict[str, str] = {
    RESULT_REF_EXPIRED: "结果引用已过期，请重新执行该步骤或重新生成结果。",
    RESULT_REF_UNAVAILABLE: "结果引用不可用（存储记录不存在或已被清理）。",
    RESULT_REF_INTEGRITY_FAILED: "结果完整性校验失败，已拒绝交付该内容。",
    RESULT_REF_SCHEMA_MISMATCH: "结果按存储时的契约版本无法解析，已降级为原始内容展示。",
    RESULT_REF_FORBIDDEN: "无权读取该结果引用。",
}

_DEFAULT_ACTIONS: dict[str, str] = {
    RESULT_REF_EXPIRED: "重新执行该步骤",
    RESULT_REF_UNAVAILABLE: "重新执行该步骤",
    RESULT_REF_INTEGRITY_FAILED: "重新执行该步骤",
    RESULT_REF_SCHEMA_MISMATCH: "按原始内容查看",
    RESULT_REF_FORBIDDEN: "联系管理员",
}

#: 完整性校验失败时对外的统一错误码（与 ``ContractErrorCode`` 对齐，便于投影）。
INTEGRITY_ERROR_CODE = ContractErrorCode.INVALID_OUTPUT.value


class ResultRef(BaseModel):
    """统一结果引用（业务层唯一可见的结果定位符）。

    字段与既有 ``{"id": ..., "sha256": ...}`` 引用**向后兼容**：旧读取方仍能只按
    ``id`` + ``sha256`` 解析；新字段（``storage_kind`` / ``schema_version`` /
    ``expires_at`` …）是附加信息，缺失时按保守默认值处理。
    """

    #: 结果 id（旧字段名沿用 ``id``，避免破坏既有快照与前端形状）。
    id: str
    #: 正文 sha256（完整性校验；空串表示未登记，读取时按"未校验"处理）。
    sha256: str = ""
    #: 分层存储位置。
    storage_kind: str = ResultStorageKind.REDIS.value
    #: 内容类型（``application/json`` / ``text/plain`` / 具体 MIME）。
    content_type: str = "application/json"
    #: 正文字节数。
    size: int = 0
    #: 所有者（用户 id）：跨用户读取一律拒绝。
    owner_id: str = ""
    job_id: str = ""
    step_id: str = ""
    #: 存储时的 payload 结构版本（解析历史数据只认它，不认当前版本）。
    schema_name: str = "execution_result"
    schema_version: int = 1
    #: 到期时间（epoch 秒；``0`` 表示未登记，按"随任务快照 TTL"处理）。
    expires_at: float = 0.0
    created_at: float = 0.0
    #: 产物引用（正文在产物存储里时给出，供降级展示与下载）。
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)

    @property
    def storage(self) -> ResultStorageKind:
        try:
            return ResultStorageKind(str(self.storage_kind))
        except ValueError:
            return ResultStorageKind.REDIS

    def expired(self, *, now: float) -> bool:
        return bool(self.expires_at) and float(now) >= float(self.expires_at)

    def as_ref_dict(self) -> dict[str, Any]:
        """写进 Job / 事件 / 快照的**最小引用**（只有 id + sha256，旧形状）。"""
        return {"id": str(self.id), "sha256": str(self.sha256)}

    def as_dict(self, *, include_artifacts: bool = False) -> dict[str, Any]:
        """完整引用（排障 / API 元数据用，不含正文）。"""
        payload: dict[str, Any] = {
            "id": str(self.id),
            "sha256": str(self.sha256),
            "storage_kind": str(self.storage_kind),
            "content_type": str(self.content_type),
            "size": int(self.size),
            "owner_id": str(self.owner_id),
            "job_id": str(self.job_id),
            "step_id": str(self.step_id),
            "schema_name": str(self.schema_name),
            "schema_version": int(self.schema_version),
            "expires_at": float(self.expires_at),
            "created_at": float(self.created_at),
        }
        if include_artifacts and self.artifact_refs:
            payload["artifact_refs"] = [item.model_dump(mode="json") for item in self.artifact_refs]
        return payload


class StoredResult(BaseModel):
    """一次写入的落库描述（正文 + 引用）；由宿主适配器构造。"""

    ref: ResultRef
    payload: str = ""

    def as_record(self) -> dict[str, Any]:
        """兼容既有 Redis 记录的载荷形状（``sha256`` / ``body`` / ``storage``）。"""
        body = json.loads(self.payload) if self.payload else None
        return {
            "sha256": str(self.ref.sha256),
            "body": body,
            "ref": self.ref.as_dict(include_artifacts=True),
            "created_at": float(self.ref.created_at),
        }


# ── 分层决策 ─────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TieringPolicy:
    """结果分层存储策略（按**序列化字节数**判定，不按字符数）。"""

    #: 不超过它就进 Redis（短摘要、错误码、少量结构化结果）。
    redis_max_bytes: int = 64 * 1024
    #: 不超过它就进本地受控目录（中等结果、短期任务结果）。
    local_max_bytes: int = 4 * 1024 * 1024
    #: 超过上面两个 → Blob Store（长文本 / 大日志 / 正文 / 生成文件）。
    blob_enabled: bool = True
    #: Blob 不可用时是否允许回退到本地目录（本地开发环境）。
    blob_fallback_local: bool = True

    def decide(self, size_bytes: int, *, blob_available: bool = True) -> ResultStorageKind:
        size = max(0, int(size_bytes))
        if size <= max(0, int(self.redis_max_bytes)):
            return ResultStorageKind.REDIS
        if size <= max(0, int(self.local_max_bytes)):
            return ResultStorageKind.LOCAL
        if self.blob_enabled and blob_available:
            return ResultStorageKind.BLOB
        if self.blob_fallback_local:
            return ResultStorageKind.LOCAL
        return ResultStorageKind.LOCAL


# ── 按预算加载 ───────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LoadBudget:
    """依赖结果按需加载的预算（只读摘要 / 字段 / 分页 / 字符预算）。"""

    #: 只看这些顶层字段（空 = 全部字段）。
    fields: tuple[str, ...] = ()
    #: 只要摘要（不返回结构化正文）。
    summary_only: bool = False
    #: 列表字段最多取多少条（``0`` = 不限制）。
    page_size: int = 0
    #: 跳过前多少条（分页游标）。
    page_offset: int = 0
    #: 文本总量预算（字符；超出的文本字段被截断并标 ``truncated``）。
    max_chars: int = 0
    #: 单字段字符上限（``0`` = 用 ``max_chars``）。
    max_field_chars: int = 0

    @property
    def unlimited(self) -> bool:
        return (
            not self.fields
            and not self.summary_only
            and not self.page_size
            and not self.max_chars
            and not self.page_offset
        )


@dataclass(frozen=True, slots=True)
class BoundedLoad:
    """一次按预算加载的结果（含裁剪事实，绝不假装完整）。"""

    body: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False
    #: 被截断/被分页的字段名（调用方据此决定要不要再翻页）。
    truncated_fields: tuple[str, ...] = ()
    #: 该字段还有更多数据时给出下一段游标。
    next_offset: int = 0
    total_chars: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "body": self.body,
            "truncated": self.truncated,
            "truncated_fields": list(self.truncated_fields),
            "next_offset": self.next_offset,
            "total_chars": self.total_chars,
        }


def _text_of(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def bound_body(body: Mapping[str, Any] | None, budget: LoadBudget) -> BoundedLoad:
    """按预算裁剪结果正文（纯函数，可单测）。

    * ``fields``：字段白名单（只读指定字段）；
    * ``summary_only``：只留摘要类字段；
    * ``page_size`` / ``page_offset``：列表字段分页，并给出 ``next_offset``；
    * ``max_chars``：文本总预算，按字段顺序累积，超出即截断并标记。
    """
    source = dict(body or {})
    truncated_fields: list[str] = []
    truncated = False
    if budget.fields:
        wanted = {str(name) for name in budget.fields}
        source = {key: value for key, value in source.items() if key in wanted}
    if budget.summary_only:
        preferred = ("summary", "display", "title", "status", "error_code", "count", "has_more")
        source = {key: source[key] for key in preferred if key in source}
    next_offset = 0
    if budget.page_size and int(budget.page_size) > 0:
        size = int(budget.page_size)
        offset = max(0, int(budget.page_offset))
        for key, value in list(source.items()):
            if not isinstance(value, list):
                continue
            window = value[offset : offset + size]
            if len(value) > offset + size:
                next_offset = offset + size
                truncated = True
                truncated_fields.append(key)
            source[key] = window
    budget_chars = max(0, int(budget.max_chars or 0))
    per_field = max(0, int(budget.max_field_chars or 0)) or budget_chars
    used = 0
    if budget_chars:
        for key, value in list(source.items()):
            if isinstance(value, str):
                allowed = min(per_field, max(0, budget_chars - used))
                text = value
                if len(text) > allowed:
                    text = text[:allowed] + "…[已按预算截断]"
                    truncated = True
                    truncated_fields.append(key)
                source[key] = text
                used += len(text)
            elif isinstance(value, (list, dict)):
                text = _text_of(value)
                if used + len(text) > budget_chars:
                    allowed = max(0, budget_chars - used)
                    source[key] = {
                        "summary": text[:allowed] + "…[依赖结果总量达到上限，已省略]"
                    } if not isinstance(value, list) else [text[:allowed] + "…[已按预算截断]"]
                    truncated = True
                    truncated_fields.append(key)
                    used = budget_chars
                else:
                    used += len(text)
    return BoundedLoad(
        body=source,
        truncated=truncated,
        truncated_fields=tuple(dict.fromkeys(truncated_fields)),
        next_offset=next_offset,
        total_chars=used,
    )


# ── 后端端口（宿主实现）─────────────────────────────────────


@runtime_checkable
class ResultBlobPort(Protocol):
    """大结果 / 本地文件的读写端口（本地目录、MinIO、S3、OSS 各自实现）。"""

    async def put(self, key: str, payload: bytes) -> None: ...

    async def get(self, key: str) -> bytes | None: ...

    async def delete(self, key: str) -> None: ...

    async def exists(self, key: str) -> bool: ...


@runtime_checkable
class ResultKvPort(Protocol):
    """小结果的键值端口（Redis / 内存）。"""

    async def put(self, key: str, payload: str, *, ttl_seconds: int) -> None: ...

    async def get(self, key: str) -> str | None: ...

    async def delete(self, key: str) -> None: ...


def digest_bytes(payload: bytes | str) -> str:
    """正文 sha256（统一实现，避免各处各算一份）。"""
    raw = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    return hashlib.sha256(raw).hexdigest()


def canonical_json(value: Any) -> str:
    """稳定的 JSON 编码（键序固定 → sha256 可复现）。"""
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"), sort_keys=True)


def resolve_expiry(*, created_at: float, ttl_seconds: int) -> float:
    """到期时间（epoch 秒）；``ttl_seconds <= 0`` 表示不过期（返回 0）。"""
    if int(ttl_seconds) <= 0:
        return 0.0
    return float(created_at) + int(ttl_seconds)


def ref_from_mapping(value: Any) -> ResultRef | None:
    """把遗留引用（dict / 模型 / ``{id, sha256}``）归一为 :class:`ResultRef`。"""
    if value is None:
        return None
    if isinstance(value, ResultRef):
        return value
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        payload = {key: value[key] for key in value if key in ResultRef.model_fields}
        if not str(payload.get("id") or ""):
            return None
        return ResultRef.model_validate(payload)
    result_id = str(getattr(value, "id", "") or "")
    if not result_id:
        return None
    return ResultRef(
        id=result_id,
        sha256=str(getattr(value, "sha256", "") or ""),
        storage_kind=str(getattr(value, "storage_kind", "") or ResultStorageKind.REDIS.value),
    )


def expires_at_iso(ref: ResultRef) -> str:
    """引用到期时间的 ISO 形式（API/元数据展示用；未登记返回空串）。"""
    return iso_utc(ref.expires_at) if ref.expires_at else ""


def expires_at_from_iso(text: object) -> float:
    return parse_iso_utc(text)


def describe_ref(ref: ResultRef | Mapping[str, Any] | None) -> str:
    """一行引用摘要（日志/审计用，不含正文）。"""
    normalized = ref_from_mapping(ref)
    if normalized is None:
        return "<无引用>"
    return (
        f"{normalized.id[:12]}… kind={normalized.storage_kind} size={normalized.size}B "
        f"schema={normalized.schema_name}@{normalized.schema_version}"
    )


def sequence_within_budget(items: Sequence[Any], max_chars: int) -> tuple[list[Any], bool]:
    """按字符预算取列表前缀（返回 ``(保留项, 是否截断)``）。"""
    if max_chars <= 0:
        return list(items), False
    out: list[Any] = []
    used = 0
    for item in items:
        size = len(_text_of(item))
        if used + size > max_chars:
            return out, True
        out.append(item)
        used += size
    return out, False


__all__ = [
    "INTEGRITY_ERROR_CODE",
    "RESULT_REF_EXPIRED",
    "RESULT_REF_FIELDS",
    "RESULT_REF_FORBIDDEN",
    "RESULT_REF_INTEGRITY_FAILED",
    "RESULT_REF_SCHEMA_MISMATCH",
    "RESULT_REF_UNAVAILABLE",
    "BoundedLoad",
    "LoadBudget",
    "ResultBlobPort",
    "ResultDegradation",
    "ResultKvPort",
    "ResultRef",
    "ResultRefError",
    "ResultStorageKind",
    "StoredResult",
    "TieringPolicy",
    "bound_body",
    "canonical_json",
    "describe_ref",
    "digest_bytes",
    "expires_at_from_iso",
    "expires_at_iso",
    "ref_from_mapping",
    "resolve_expiry",
    "sequence_within_budget",
]
