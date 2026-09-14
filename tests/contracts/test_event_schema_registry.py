"""事件 Schema 注册表 + 未知事件策略（方案 §1.3）回归。

锁死三件事：

1. **注册维度是 (message_type, schema_version)**：每种标准事件都有登记版本，
   信封帧携带同一个版本号；
2. **只允许加法兼容**：同 type 的版本链删字段/改类型必须报错（CI 强制）；
3. **未知事件不解析正文**：小数据降级为空载荷并打 ``unsupported`` 标记，
   大对象只留 ``data_ref`` 哈希引用——前端记录"客户端不支持该事件"，不白屏。
"""

from __future__ import annotations

import pytest
from pydantic import Field

from lumi_contracts import (
    CANONICAL_EVENT_TYPES,
    PAYLOAD_SCHEMA_VERSIONS,
    build_envelope,
    current_schema_version,
    decide_unknown_event,
    is_registered,
    schema_diff,
    schema_model,
)
from lumi_contracts.events.envelope import EventPayload, TextDeltaPayload
from lumi_contracts.events.registry import assert_additive_only, registered_types


class _TextDeltaV2(TextDeltaPayload):
    """加法兼容的新版本（多一个可选字段）。"""

    emphasis: str = ""


class _TextDeltaBroken(EventPayload):
    """破坏性变更：把 ``content`` 删掉（新版本不再是旧版本超集）。"""

    format: str = "markdown"
    message_id: str = ""


class _TextDeltaRetyped(EventPayload):
    """破坏性变更：把 ``content`` 改成别的类型。"""

    content: int = 0  # type: ignore[assignment]
    format: str = "markdown"
    message_id: str = ""


# ── 1. 注册表 ────────────────────────────────────────────


def test_registry_covers_every_canonical_type_exactly_once():
    types = {event_type for event_type, _version in __import__(
        "lumi_contracts.events.registry", fromlist=["SCHEMA_REGISTRY"]
    ).SCHEMA_REGISTRY}
    assert types == set(CANONICAL_EVENT_TYPES)
    assert registered_types() == frozenset(CANONICAL_EVENT_TYPES)


def test_registry_is_keyed_by_type_and_version():
    assert schema_model("text_delta", 1) is TextDeltaPayload
    assert schema_model("text_delta") is TextDeltaPayload, "不给版本时按当前版本"
    assert schema_model("text_delta", 99) is None, "未登记的版本必须返回 None"
    assert schema_model("not_a_type") is None
    assert current_schema_version("error") == 1
    assert all(version == 1 for version in PAYLOAD_SCHEMA_VERSIONS.values())
    assert is_registered("control", 1) and not is_registered("control", 2)


def test_envelope_carries_the_registered_schema_version():
    envelope = build_envelope("delta", {"content": "hi"}, job_id="j1")
    frame = envelope.to_canonical_frame()
    assert frame["type"] == "text_delta"
    assert frame["schema_version"] == current_schema_version("text_delta")
    control = build_envelope("done", {"state": "completed"}, job_id="j1").to_canonical_frame()
    assert control["schema_version"] == current_schema_version("control")


# ── 2. 加法兼容校验 ──────────────────────────────────────


def test_schema_diff_reports_additions_removals_and_retypes():
    added = schema_diff(TextDeltaPayload, _TextDeltaV2)
    assert added.added == ("emphasis",) and added.additive
    removed = schema_diff(TextDeltaPayload, _TextDeltaBroken)
    assert removed.removed == ("content",) and not removed.additive
    retyped = schema_diff(TextDeltaPayload, _TextDeltaRetyped)
    assert retyped.retyped == ("content",) and not retyped.additive


def test_additive_only_check_accepts_additions_and_rejects_breakage():
    assert_additive_only("text_delta", [(1, TextDeltaPayload), (2, _TextDeltaV2)])
    with pytest.raises(ValueError, match="不是加法兼容变更"):
        assert_additive_only("text_delta", [(1, TextDeltaPayload), (2, _TextDeltaBroken)])
    with pytest.raises(ValueError, match="不是加法兼容变更"):
        assert_additive_only("text_delta", [(1, TextDeltaPayload), (2, _TextDeltaRetyped)])


def test_every_registered_chain_is_additive():
    """CI 校验：所有已登记 type 的版本链必须只做加法（当前都是单版本 → 必然通过）。"""
    from lumi_contracts.events.registry import SCHEMA_REGISTRY

    chains: dict[str, list[tuple[int, type[EventPayload]]]] = {}
    for (event_type, version), model in SCHEMA_REGISTRY.items():
        chains.setdefault(event_type, []).append((version, model))
    for event_type, versions in chains.items():
        assert_additive_only(event_type, versions)


def test_payload_models_ignore_unknown_fields_but_new_versions_are_declared():
    """新增可选字段不升版本（前端不必同步发布），但必须走 ``extra="ignore"``。"""
    payload = TextDeltaPayload.model_validate({"content": "x", "brand_new": 1})
    assert payload.content == "x"
    assert not hasattr(payload, "brand_new")
    assert _TextDeltaV2.model_fields["emphasis"].default == Field("").default


# ── 3. 未知事件策略 ──────────────────────────────────────


def test_known_type_passes_through():
    decision = decide_unknown_event("text_delta", {"content": "hi"})
    assert decision.action == "pass"
    assert decision.payload == {}


def test_unknown_type_with_whitelisted_keys_passes():
    decision = decide_unknown_event("future_type", {"capability": "x"})
    assert decision.action == "pass"


def test_unknown_structure_small_payload_is_not_parsed():
    decision = decide_unknown_event("future_type", {"brand_new_blob": "x" * 100})
    assert decision.action == "empty"
    assert decision.payload == {"unsupported": True, "schema_version": 0}
    assert decision.reason


def test_unknown_structure_large_payload_becomes_opaque_ref_only():
    decision = decide_unknown_event("future_type", {"brand_new_blob": "x" * 9000})
    assert decision.action == "ref"
    assert decision.payload["unsupported"] is True
    assert decision.payload["data_ref"].startswith("opaque:future_type:")
    assert "x" * 9000 not in decision.payload["data_ref"], "只留哈希引用，不搬正文"
    assert decision.payload["size_bytes"] > 4096


def test_adapter_applies_unknown_policy_end_to_end():
    from app.contracts.event_adapter import canonical_payload

    kept = canonical_payload("future_type", {"type": "future_type", "capability": "x", "blob": {"a": 1}})
    assert kept["capability"] == "x", "已登记的安全字段仍然透传（前端容忍未知类型）"
    assert kept["unsupported"] is True, "但必须打标记，避免被当成已知事件渲染"

    dropped = canonical_payload("future_type", {"type": "future_type", "blob": {"a": 1}})
    assert dropped == {"unsupported": True, "schema_version": 0}

    referenced = canonical_payload("future_type", {"type": "future_type", "blob": "y" * 9000})
    assert referenced["data_ref"].startswith("opaque:future_type:")
