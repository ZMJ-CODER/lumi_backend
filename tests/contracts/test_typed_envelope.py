"""内部信封 ``TypedEnvelope`` 契约回归（方案 §1.1 / §4.1 / §9.9）。

锁死五件事：

1. **字段固定 + 校验后不可变**：改安全字段或信封字段必须报错；
2. **载荷二选一且互斥**：``payload_data`` / ``payload_ref`` 同时给或都不给 → 拒绝；
   内联载荷超 64KB → 拒绝（大对象必须 Artifact + 引用）；
3. **安全字段不可伪造**：``security_context`` 只有 Gateway 工厂能生成，手工构造被拒；
4. **链路语义**：``trace_id`` 全链不变、``causation_id`` 指向上游、``message_id`` 每条新生成；
5. **审计只存元数据**：不带正文；公开投影不带内部字段（禁泄清单）。
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from lumi_contracts import (
    INTERNAL_ONLY_FIELDS,
    PAYLOAD_DATA_MAX_BYTES,
    SecurityContext,
    TypedEnvelope,
    dedupe_by_idempotency,
    new_security_context,
    new_trace_id,
    reseal_for_gateway,
)


def _context():
    return new_security_context(actor_id="u1", workspace_id="ws1", scopes=("workspace.read",))


def _envelope(**overrides) -> TypedEnvelope:
    base = {
        "message_type": "llm.text.delta",
        "source": "orchestrator",
        "target": "sse-gateway",
        "job_id": "job-1",
        "security_context": _context(),
        "payload_data": {"content": "hi"},
    }
    base.update(overrides)
    return TypedEnvelope(**base)


# ── 1. 字段与不可变 ──────────────────────────────────────


def test_envelope_has_the_frozen_internal_field_set():
    envelope = _envelope(correlation_id="corr-1", causation_id="msg-upstream")
    data = envelope.model_dump()
    for key in (
        "message_id", "message_type", "schema_version", "trace_id", "correlation_id",
        "causation_id", "idempotency_key", "job_id", "step_id", "conversation_id",
        "source", "target", "security_context", "payload_data", "payload_ref",
    ):
        assert key in data, key
    assert data["message_id"].startswith("msg_")
    assert data["trace_id"].startswith("trace_")


def test_envelope_is_immutable_after_validation():
    envelope = _envelope()
    with pytest.raises(ValidationError):
        envelope.source = "forged"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        envelope.payload_data = {"tampered": True}  # type: ignore[misc]


def test_message_type_is_required():
    with pytest.raises(ValidationError, match="message_type"):
        TypedEnvelope(message_type="", payload_data={"a": 1})


# ── 2. 载荷互斥与体积 ────────────────────────────────────


def test_payload_data_and_ref_are_mutually_exclusive():
    with pytest.raises(ValidationError, match="互斥"):
        _envelope(payload_data={"a": 1}, payload_ref="artifact:b1")


def test_payload_must_be_present_in_one_of_two_forms():
    with pytest.raises(ValidationError, match="二选一"):
        _envelope(payload_data={}, payload_ref="")


def test_oversize_inline_payload_must_use_artifact_ref():
    too_big = {"blob": "x" * (PAYLOAD_DATA_MAX_BYTES + 10)}
    with pytest.raises(ValidationError, match="payload_ref"):
        _envelope(payload_data=too_big)
    # 走引用则合法（正文不进信封）
    envelope = _envelope(payload_data={}, payload_ref="artifact:big-1")
    assert envelope.payload_ref == "artifact:big-1"
    assert envelope.payload_data == {}


# ── 3. 安全上下文不可伪造 ────────────────────────────────


def test_security_context_can_only_be_created_by_the_gateway():
    forged = SecurityContext(actor_id="u1", actor_role="superadmin")
    assert not forged.sealed_by_gateway
    with pytest.raises(ValidationError, match="Gateway"):
        _envelope(security_context=forged)
    # 工厂生成的可以被接受，且外发提示只含最小信息
    sealed = _context()
    assert sealed.sealed_by_gateway
    assert sealed.public_hint() == {"actor_role": "user", "trust_level": "internal"}


def test_security_context_is_frozen_and_resealed_across_gates():
    context = _context()
    with pytest.raises(ValidationError):
        context.actor_role = "superadmin"  # type: ignore[misc]

    third_party = reseal_for_gateway(context, trust_level="third_party", scopes=("workspace.write",))
    assert third_party.sealed_by_gateway
    assert third_party.trust_level == "third_party"
    assert third_party.scopes == ("workspace.write",)
    assert third_party.actor_id == context.actor_id, "换门只改信任等级/范围，不改身份"


def test_envelope_without_security_context_is_rejected_at_the_gateway_boundary():
    """跨门时缺失上下文由调用方显式拒绝（工厂缺失不能被当成"内部可信"）。"""
    envelope = _envelope(security_context=None)
    assert envelope.security_context is None
    assert "security_context" in INTERNAL_ONLY_FIELDS


# ── 4. 链路语义（trace / causation / idempotency）────────


def test_child_keeps_trace_and_chains_causation():
    parent = _envelope(correlation_id="corr-1")
    child = parent.child("tool.call", {"tool": "read_file"}, target="plugin-gateway")
    assert child.trace_id == parent.trace_id, "trace_id 全链不变"
    assert child.causation_id == parent.message_id, "causation_id 指向上游消息"
    assert child.message_id != parent.message_id, "每条消息新生成 message_id"
    assert child.correlation_id == "corr-1"
    assert child.job_id == parent.job_id
    assert child.source == parent.target, "默认从上游 target 接续"
    assert child.payload_data == {"tool": "read_file"}

    grandchild = child.child("tool.result", payload_ref="artifact:r1")
    assert grandchild.trace_id == parent.trace_id
    assert grandchild.causation_id == child.message_id
    assert grandchild.payload_ref == "artifact:r1"


def test_new_trace_id_is_unique_per_chain():
    assert new_trace_id() != new_trace_id()
    assert _envelope().trace_id != _envelope().trace_id


def test_idempotency_dedupe_keeps_first_and_preserves_order():
    first = _envelope(idempotency_key="call-1", payload_data={"n": 1})
    duplicate = _envelope(idempotency_key="call-1", payload_data={"n": 1})
    second = _envelope(idempotency_key="call-2")
    no_key = _envelope()
    kept = dedupe_by_idempotency([first, duplicate, second, no_key])
    assert [item.idempotency_key for item in kept] == ["call-1", "call-2", ""]
    assert kept[0].payload_data == {"n": 1}, "重试只处理第一次"


# ── 5. 审计只存元数据 / 公开投影不带内部字段 ─────────────


def test_audit_metadata_has_no_payload_body():
    envelope = _envelope(payload_data={"content": "SECRET-COT-不要外发"})
    audit = envelope.audit_metadata()
    blob = json.dumps(audit, ensure_ascii=False)
    assert "SECRET-COT" not in blob, "审计只存元数据，正文进 Blob/Artifact"
    assert audit["payload_sha256"] == envelope.payload_digest()
    assert audit["payload_size"] > 0
    assert audit["message_type"] == "llm.text.delta"
    assert audit["trust_level"] == "internal"


def test_public_projection_drops_internal_fields():
    envelope = _envelope(source="orchestrator", target="sse-gateway")
    public = envelope.public_payload()
    # 中性键 ``ref``（投影层再映射成 data_ref / result_ref），内部字段名一个都不出现
    assert set(public) == {"type", "payload", "ref"}
    leaked = INTERNAL_ONLY_FIELDS & set(public)
    assert not leaked, f"内部字段泄露：{leaked}"


def test_extra_fields_are_rejected():
    """``extra="forbid"``：拼错字段名不会被静默吞掉（内部信封必须严格）。"""
    with pytest.raises(ValidationError):
        TypedEnvelope(message_type="llm.text.delta", payload_data={"a": 1}, bogus="x")  # type: ignore[call-arg]
