"""内部信封 ``TypedEnvelope``（**后端专用，永不出后端**）。

与公开事件（:mod:`lumi_contracts.events.envelope` 的 ``EventEnvelope``）严格分层：

* 内部信封带 ``source`` / ``target`` / ``security_context`` / ``causation_id`` /
  ``idempotency_key`` ——这些**禁止进入公开事件**（方案 §1.2 禁泄清单）；
* 内部信封的载荷是 ``payload_data``（小数据内联）**或** ``payload_ref``
  （大对象只留 Artifact 引用），**互斥**且不可同时出现（结构性约束，构造即校验）；
* ``security_context`` **只能由 Gateway 工厂生成**（:func:`new_security_context`）：
  组件自己拼一个（哪怕是同名字段）会在构造时被拒，这就是"组件不可伪造安全字段"；
* 信封校验后**不可变**（``frozen=True``）；派生消息只能经 :meth:`TypedEnvelope.child`
  生成，``trace_id`` 全链不变、``causation_id`` 指向上游 ``message_id``。
* 审计只落**元数据**（id / hash / size / status）：正文进 Blob/Artifact（见
  :meth:`TypedEnvelope.audit_metadata`）。

跨信任域（A 门 / B 门 / SSE Gateway）时必须**重新生成或重新校验** ``security_context``，
不得直接继承上游的（:func:`reseal_for_gateway`）。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: 内部信封契约版本（与公开事件的 ``version`` 分开管理）。
TYPED_ENVELOPE_VERSION = 1

#: 内联载荷上限：超过即必须走 ``payload_ref``（大对象一律 Artifact）。
PAYLOAD_DATA_MAX_BYTES = 65_536

#: 禁止出现在公开事件里的内部信封字段（投影层与安全验收共用这一份清单）。
INTERNAL_ONLY_FIELDS: frozenset[str] = frozenset({
    "source", "target", "security_context", "correlation_id", "causation_id",
    "idempotency_key", "payload_data", "payload_ref", "message_id", "message_type",
})

#: Gateway 工厂印章：组件无法伪造（值不参与校验，参与校验的是"是否由工厂设置"）。
_GATEWAY_SEAL = "lumi-gateway-v1"

TrustLevel = Literal["internal", "trusted_plugin", "third_party", "user"]


def make_message_id() -> str:
    """每条消息新生成的 ``message_id``（与全链不变的 ``trace_id`` 区分开）。"""
    return f"msg_{uuid.uuid4().hex}"


def new_trace_id() -> str:
    """新链路 id（跨 A 门/B 门/SSE 都不变）。"""
    return f"trace_{uuid.uuid4().hex}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _payload_size(value: Any) -> int:
    try:
        return len(json.dumps(value or {}, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


class SecurityContext(BaseModel):
    """安全上下文：**只能**由 :func:`new_security_context` 生成。

    ``frozen=True``：校验通过后不可修改（业务组件不得改写安全字段）。
    ``_gateway_seal`` 是"经过 Gateway 生成"的证明；手工构造的同名对象在
    :class:`TypedEnvelope` 校验时会被拒绝。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    actor_id: str = ""
    actor_role: str = "user"
    workspace_id: str = ""
    trust_level: TrustLevel = "internal"
    scopes: tuple[str, ...] = ()
    issued_at: str = ""
    gateway_seal: str = ""

    @property
    def sealed_by_gateway(self) -> bool:
        return self.gateway_seal == _GATEWAY_SEAL

    def public_hint(self) -> dict[str, Any]:
        """可外发的**最小**信息（公开事件里最多只放这个，绝不放整份上下文）。"""
        return {"actor_role": self.actor_role, "trust_level": self.trust_level}


def new_security_context(
    *,
    actor_id: str = "",
    actor_role: str = "user",
    workspace_id: str = "",
    trust_level: TrustLevel = "internal",
    scopes: tuple[str, ...] | list[str] = (),
) -> SecurityContext:
    """Gateway 工厂：跨信任域时**唯一**允许生成 ``SecurityContext`` 的入口。"""
    return SecurityContext(
        actor_id=str(actor_id or ""),
        actor_role=str(actor_role or "user"),
        workspace_id=str(workspace_id or ""),
        trust_level=trust_level,
        scopes=tuple(str(item) for item in scopes),
        issued_at=_now_iso(),
        gateway_seal=_GATEWAY_SEAL,
    )


def reseal_for_gateway(
    context: SecurityContext,
    *,
    trust_level: TrustLevel,
    scopes: tuple[str, ...] | list[str] | None = None,
    actor_role: str = "",
) -> SecurityContext:
    """跨门时**重新签发**：调用方声明新的信任等级/权限范围，而不是继承上游。"""
    return new_security_context(
        actor_id=context.actor_id,
        actor_role=actor_role or context.actor_role,
        workspace_id=context.workspace_id,
        trust_level=trust_level,
        scopes=tuple(scopes) if scopes is not None else context.scopes,
    )


class TypedEnvelope(BaseModel):
    """内部消息信封（固定字段 + 注册式载荷，校验后不可变）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # ── 身份与版本 ──
    message_id: str = Field(default_factory=make_message_id)
    message_type: str = ""
    schema_version: int = 1
    envelope_version: int = TYPED_ENVELOPE_VERSION
    # ── 链路（trace 全链不变，causation 指向上游） ──
    trace_id: str = Field(default_factory=new_trace_id)
    correlation_id: str = ""
    causation_id: str = ""
    idempotency_key: str = ""
    # ── 业务定位 ──
    job_id: str = ""
    step_id: str = ""
    conversation_id: str = ""
    occurred_at: str = Field(default_factory=_now_iso)
    # ── 内部路由 + 安全 ──
    source: str = ""
    target: str = ""
    security_context: SecurityContext | None = None
    # ── 载荷（互斥） ──
    payload_data: dict[str, Any] = Field(default_factory=dict)
    payload_ref: str = ""
    status: str = "ok"

    @model_validator(mode="after")
    def _validate(self) -> "TypedEnvelope":
        if not str(self.message_type or "").strip():
            raise ValueError("TypedEnvelope 必须声明 message_type")
        if self.payload_ref and self.payload_data:
            raise ValueError("payload_data 与 payload_ref 互斥：大对象只留引用")
        if not self.payload_ref and not self.payload_data:
            raise ValueError("payload_data 与 payload_ref 必须二选一")
        size = _payload_size(self.payload_data)
        if size > PAYLOAD_DATA_MAX_BYTES:
            raise ValueError(
                f"内联载荷 {size} 字节超过 {PAYLOAD_DATA_MAX_BYTES}：请改走 Artifact payload_ref"
            )
        if self.security_context is not None and not self.security_context.sealed_by_gateway:
            raise ValueError("security_context 必须由 Gateway 工厂生成（组件不可伪造安全字段）")
        return self

    # ── 派生 ─────────────────────────────────────────────

    def child(
        self,
        message_type: str,
        payload: dict[str, Any] | None = None,
        *,
        payload_ref: str = "",
        source: str = "",
        target: str = "",
        schema_version: int = 1,
        idempotency_key: str = "",
        step_id: str = "",
    ) -> "TypedEnvelope":
        """派生下游消息：``trace_id`` 不变、``causation_id`` 指向本消息、新 ``message_id``。"""
        return TypedEnvelope(
            message_type=message_type,
            schema_version=int(schema_version),
            trace_id=self.trace_id,
            correlation_id=self.correlation_id or self.message_id,
            causation_id=self.message_id,
            idempotency_key=str(idempotency_key or ""),
            job_id=self.job_id,
            step_id=step_id or self.step_id,
            conversation_id=self.conversation_id,
            source=source or self.target or self.source,
            target=target,
            # 跨进程/跨门时必须重新签发；同进程内派生沿用本门已校验过的上下文。
            security_context=self.security_context,
            payload_data=dict(payload or {}) if not payload_ref else {},
            payload_ref=str(payload_ref or ""),
        )

    # ── 审计（只存元数据） ───────────────────────────────

    def payload_digest(self) -> str:
        """载荷内容哈希（审计/幂等用；不落正文）。"""
        try:
            blob = json.dumps(self.payload_data or {}, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            blob = self.payload_ref or ""
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def payload_size(self) -> int:
        return _payload_size(self.payload_data)

    def audit_metadata(self) -> dict[str, Any]:
        """审计记录：**只有元数据**（id / hash / size / status），正文进 Blob/Artifact。"""
        return {
            "message_id": self.message_id,
            "message_type": self.message_type,
            "schema_version": int(self.schema_version),
            "trace_id": self.trace_id,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "idempotency_key": self.idempotency_key,
            "job_id": self.job_id,
            "step_id": self.step_id,
            "conversation_id": self.conversation_id,
            "source": self.source,
            "target": self.target,
            "trust_level": self.security_context.trust_level if self.security_context else "",
            "payload_sha256": self.payload_digest(),
            "payload_size": self.payload_size(),
            "payload_ref": self.payload_ref,
            "status": self.status,
            "occurred_at": self.occurred_at,
        }

    def public_payload(self) -> dict[str, Any]:
        """交给投影层的**可外发**部分：内部字段名一律不带（禁泄清单见模块 docstring）。

        ``payload_ref`` 换成中性键 ``ref``：投影层再决定它落到公开事件的
        ``data_ref`` / ``result_ref`` / ``artifact_refs``（公开事件里没有"内部引用"这个概念）。
        """
        return {
            "type": self.message_type,
            "payload": dict(self.payload_data or {}),
            "ref": self.payload_ref,
        }


def dedupe_by_idempotency(envelopes: list[TypedEnvelope]) -> list[TypedEnvelope]:
    """按 ``idempotency_key`` 去重（保序；无键的照常保留）。

    重试/插件重启后同一次调用只被处理一次（方案 §4.2 crash 策略复用幂等键）。
    """
    seen: set[str] = set()
    out: list[TypedEnvelope] = []
    for envelope in envelopes or []:
        key = envelope.idempotency_key
        if key:
            if key in seen:
                continue
            seen.add(key)
        out.append(envelope)
    return out


__all__ = [
    "INTERNAL_ONLY_FIELDS",
    "PAYLOAD_DATA_MAX_BYTES",
    "TYPED_ENVELOPE_VERSION",
    "SecurityContext",
    "TrustLevel",
    "TypedEnvelope",
    "dedupe_by_idempotency",
    "make_message_id",
    "new_security_context",
    "new_trace_id",
    "reseal_for_gateway",
]

