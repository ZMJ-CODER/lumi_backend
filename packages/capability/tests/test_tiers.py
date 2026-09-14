"""档位派生的纯函数测试：安全边界，宁可多测。"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from lumi_capability import tiers as t


# ── 1. 副作用 → 档位 ────────────────────────────────────────


def test_no_declared_effect_returns_empty_not_auto():
    """**什么都不声明就不猜**：返回空串让调用方走保守默认，而不是默认成 auto。"""
    assert t.side_effect_tier([]) == ""
    assert t.side_effect_tier(None) == ""
    assert t.side_effect_tier([""]) == ""


@pytest.mark.parametrize(
    "effect,expected",
    [("read", "auto"), ("write", "routine"), ("delete", "routine"),
     ("execute", "routine"), ("network", "routine"), ("external", "critical")],
)
def test_contract_side_effect_vocabulary_is_covered(effect, expected):
    assert t.side_effect_tier([effect]) == expected


def test_critical_dominates_routine_and_auto():
    assert t.side_effect_tier(["read", "write", "external"]) == "critical"
    assert t.side_effect_tier(["read", "write"]) == "routine"
    assert t.side_effect_tier(["read"]) == "auto"


def test_enum_like_effects_are_accepted():
    """契约枚举与字符串混用都要认（``getattr(item, "value", item)``）。"""

    class Kind:
        def __init__(self, value: str) -> None:
            self.value = value

    assert t.side_effect_tier([Kind("external")]) == "critical"


def test_unknown_effect_falls_back_to_routine_not_auto():
    """未知副作用按例行确认处理：宁可多问一次，不可静默执行。"""
    assert t.side_effect_tier(["teleport"]) == "routine"


def test_custom_table_extends_contract_vocabulary():
    """应用侧可以叠加自己的合成副作用名（本项目有 ``publish``）。"""
    table = {**t.SIDE_EFFECT_TIER, "publish": t.TIER_CRITICAL}
    assert t.side_effect_tier(["publish"], table=table) == "critical"
    assert t.side_effect_tier(["publish"]) == "routine"  # 不传表时按未知处理


# ── 2. 合并与归一 ───────────────────────────────────────────


def test_stricter_takes_the_more_severe_side():
    assert t.stricter("auto", "critical") == "critical"
    assert t.stricter("routine", "") == "routine"
    assert t.stricter("", "") == ""
    assert t.stricter("bogus", "auto") == "auto"  # 非法值 = 没有意见


def test_normalize_tier_rejects_junk():
    assert t.normalize_tier("CRITICAL") == "critical"
    assert t.normalize_tier(" Auto ") == "auto"
    assert t.normalize_tier("bogus") == ""
    assert t.normalize_tier(None) == ""


def test_merge_declared_only_tightens():
    assert t.merge_declared("auto", ["routine"]) == "routine"
    assert t.merge_declared("critical", ["auto"]) == "critical"
    assert t.merge_declared("", ["routine"]) == "routine"
    assert t.merge_declared("", []) == ""


# ── 3. "本机必须确认"只抬不降 ───────────────────────────────


def test_local_confirmation_lifts_auto_to_routine():
    assert t.floor_for_local_confirmation("auto", True) == "routine"
    assert t.floor_for_local_confirmation("auto", False) == "auto"
    assert t.floor_for_local_confirmation("critical", True) == "critical"


def test_descriptor_declared_tier_lifts_auto():
    class Descriptor:
        side_effects = ["read"]
        needs_local_confirmation = True

    assert t.descriptor_declared_tier(Descriptor()) == "routine"


def test_descriptor_without_declaration_returns_empty():
    class Empty:
        side_effects = []
        needs_local_confirmation = False

    assert t.descriptor_declared_tier(Empty()) == ""


# ── 4. Manifest 声明 ────────────────────────────────────────


@dataclass
class _Permission:
    needs_local_confirmation: bool = False


@dataclass
class _Manifest:
    side_effects: list[str] = field(default_factory=list)
    needs_approval: bool = False
    permissions: list[_Permission] = field(default_factory=list)


def test_manifest_tier_uses_side_effects_and_permissions():
    manifest = _Manifest(side_effects=["write"], permissions=[_Permission(True)])
    assert t.manifest_tier_of(manifest) == "routine"
    assert t.manifest_requires_local_confirmation(manifest) is True


def test_manifest_needs_approval_forces_confirmation():
    manifest = _Manifest(side_effects=["read"], needs_approval=True)
    assert t.manifest_requires_local_confirmation(manifest) is True
    assert t.manifest_tier_of(manifest) == "routine"  # auto 被抬档


def test_manifest_without_declaration_returns_empty():
    assert t.manifest_tier_of(_Manifest()) == ""


def test_broken_manifest_property_counts_as_needs_confirmation():
    """读不出来的自述属性**不能**变成"无需确认"（fail-closed）。"""

    class Broken:
        @property
        def needs_approval(self):
            raise RuntimeError("boom")

    assert t.manifest_requires_local_confirmation(Broken()) is True
