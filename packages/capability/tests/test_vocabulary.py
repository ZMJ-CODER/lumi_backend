"""统一能力词表的纯函数测试（不 import 应用，见包 docstring 的边界声明）。"""

from __future__ import annotations

import pytest

from lumi_capability import vocabulary as v


def test_unified_capability_set_is_closed():
    """七个统一能力：五个资源动作 + 执行 + 产物。多一个少一个都是协议变更。"""
    assert v.UNIFIED_CAPABILITIES == frozenset(
        {
            "resource.read",
            "resource.write",
            "resource.edit",
            "resource.move",
            "resource.delete",
            "code.execute",
            "artifact.create",
        }
    )


def test_resource_types_are_closed():
    assert v.RESOURCE_TYPES == frozenset(
        {"workspace", "office_document", "knowledge", "artifact", "memory"}
    )


@pytest.mark.parametrize(
    "alias,expected",
    [("sandbox.run", "code.execute"), ("resource.search", "resource.read")],
)
def test_aliases_collapse_to_existing_capability(alias, expected):
    """别名是"同一件事的另一种写法"，不是新能力——否则模型会看到两个同义动词。"""
    assert v.normalize_unified_capability(alias) == expected
    assert v.is_unified_capability(alias) is True


def test_unknown_name_passes_through_and_is_not_unified():
    assert v.normalize_unified_capability("workspace.write") == "workspace.write"
    assert v.is_unified_capability("workspace.write") is False


def test_empty_and_whitespace_normalize_to_empty():
    assert v.normalize_unified_capability("") == ""
    assert v.normalize_unified_capability("   ") == ""
    assert v.is_unified_capability("") is False


def test_canonical_names_are_already_unified():
    for name in v.UNIFIED_CAPABILITIES:
        assert v.normalize_unified_capability(name) == name
        assert v.is_unified_capability(name) is True


def test_aliases_point_at_real_capabilities():
    """别名表不许指向不存在的能力（漂移的契约比没有契约更糟）。"""
    assert set(v.UNIFIED_CAPABILITY_ALIASES.values()) <= v.UNIFIED_CAPABILITIES
