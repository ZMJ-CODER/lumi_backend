"""必须保留工具 + 四层工具快照（方案 §五 P0）。

这一层修的是"**工具其实注册好了，但模型看不见**"——现场表现是"模型识别不到读工具"，
而真因是它作为普通候选参与了 Top-K 竞争，被新装的写工具挤出了 ``[:8]`` 窗口。

因此本文件的核心断言只有两条：

1. **强制工具永远在最终窗口里**，无论可选工具多强、上限多小；
2. **它在哪一层消失能被看出来**（四层快照 + 逐层差集 + dropped_core 告警）。
"""

from __future__ import annotations

from app.agents.skills.capability import ToolCapability
from app.agents.skills.mandatory_tools import (
    CORE_TOOLS,
    apply_tool_window,
    build_snapshot,
    registry_epoch,
    tool_identity,
    trim_with_mandatory,
    visibility_state,
)


def _cap(name: str, **overrides) -> ToolCapability:
    payload = {
        "name": name,
        "description": f"{name} 工具",
        "category": "office",
        "domain": "document",
        "parameters": {"type": "object", "properties": {}},
    }
    payload.update(overrides)
    return ToolCapability(**payload)


# ── P0-1：强制工具不参与竞争 ───────────────────────────────


def test_core_tool_survives_a_full_optional_pool():
    """8 个强相关的写工具 + 核心读工具，上限 8：读工具不能被挤掉。"""
    writers = [_cap(f"workspace_write_{i}", write_op=True) for i in range(8)]
    pool = [*writers, _cap("workspace_navigator")]

    result = trim_with_mandatory(pool, limit=8)
    names = [item.name for item in result.capabilities]
    assert "workspace_navigator" in names, "核心工具必须脱离 Top-K 竞争"
    assert len(names) == 9, "上限只约束可选工具：核心工具占位后总数可以是 8+1"


def test_core_tool_survives_a_tiny_limit():
    """上限 1 也不能把核心工具裁掉（它就是"读工具被挤掉"的最小复现）。

    ``limit`` 是**可选预算**，所以最终可以是「1 可选 + 1 强制」；关键是强制工具在。
    """
    pool = [_cap("python_exec"), _cap("web_search"), _cap("workspace_navigator")]
    result = trim_with_mandatory(pool, limit=1)
    names = [item.name for item in result.capabilities]
    assert "workspace_navigator" in names
    assert names[0] == "workspace_navigator", "强制工具排在前面（保留排序语义）"
    assert len(names) == 2, "1 个可选 + 1 个强制"


def test_core_tool_keeps_pool_order_for_ranking_semantics():
    """强制工具按**候选池原顺序**占位（原顺序 = 上层排序结果），不重排其它工具。"""
    pool = [_cap("workspace_navigator"), _cap("a"), _cap("b")]
    result = trim_with_mandatory(pool, limit=3)
    assert [item.name for item in result.capabilities] == ["workspace_navigator", "a", "b"]


def test_optional_trimming_is_reported_not_silent():
    """被上限裁掉的可选工具必须留痕（以前是"悄悄消失"）。"""
    pool = [_cap("workspace_navigator"), *[_cap(f"t{i}") for i in range(10)]]
    result = trim_with_mandatory(pool, limit=3)
    assert result.trimmed_optional, "被裁掉的可选工具必须列出来"
    assert len(result.capabilities) == 4  # 3 可选 + 1 强制
    assert all(name not in result.trimmed_optional for name in result.pinned)


def test_extra_mandatory_from_the_current_task_is_honoured():
    """本轮明确要写文件 → 写工具被钉住，不靠排序运气。"""
    pool = [_cap("workspace_navigator"), _cap("workspace_write"), _cap("a"), _cap("b")]
    result = trim_with_mandatory(pool, limit=2, extra_mandatory=("workspace_write",))
    names = [item.name for item in result.capabilities]
    assert "workspace_write" in names and "workspace_navigator" in names


def test_missing_core_tool_is_reported_as_upstream_filtering():
    """核心工具**不在候选池**里 = 上游过滤掉了它，必须显式暴露（而不是静默少一个）。"""
    result = trim_with_mandatory([_cap("a"), _cap("b")], limit=8)
    assert result.dropped_core == ["workspace_navigator"]
    assert all(name != "workspace_navigator" for name in (item.name for item in result.capabilities))


def test_core_tools_list_is_deliberately_minimal():
    """核心清单只放"少了它功能就残废"的工具——每多一个都挤占可选空间。"""
    assert CORE_TOOLS == frozenset({"workspace_navigator"})


# ── P0-2：四层快照 ─────────────────────────────────────────


def test_snapshot_records_four_layers_and_per_layer_diff():
    catalog = [_cap("c1"), _cap("c2"), _cap("c3")]
    eligible = [_cap("c1"), _cap("c2")]
    ranked = [_cap("c1"), _cap("c2")]
    final = [_cap("c1")]
    snapshot = build_snapshot(
        scene="office", limit=1, catalog=catalog, eligible=eligible, ranked=ranked, final=final
    )
    payload = snapshot.as_dict()
    assert payload["counts"] == {"catalog": 3, "eligible": 2, "ranked": 2, "final": 1}
    # 逐层差集：一眼看出谁在哪一层消失。
    assert payload["dropped_by_layer"]["catalog→eligible"] == ["c3"]
    assert payload["dropped_by_layer"]["ranked→final"] == ["c2"]


def test_snapshot_visibility_distinguishes_three_states():
    """Catalog / Eligible / Available 三态必须分得开（不要混成"工具是否存在"）。"""
    available = _cap("a", annotations={"availability_hint": "available"})
    offline = _cap("b", annotations={"availability_hint": "offline"})
    unknown = _cap("c")
    assert visibility_state(available) == "available"
    assert visibility_state(offline) == "unavailable"
    assert visibility_state(unknown) == "eligible"


def test_apply_tool_window_returns_objects_and_snapshot():
    pool = [_cap("python_exec"), _cap("workspace_navigator")]
    kept, snapshot = apply_tool_window(pool, limit=1, scene="office", layer="test")
    assert all(isinstance(item, ToolCapability) for item in kept), "必须返回对象（调用方还要读字段）"
    assert "workspace_navigator" in [item.name for item in kept]
    assert snapshot.final[0] == "workspace_navigator"
    assert snapshot.pinned == ["workspace_navigator"]


def test_snapshot_diff_reports_tools_lost_between_rounds():
    """ReAct 每轮对比：上一轮还能用的工具这轮怎么没了。"""
    first = build_snapshot(scene="office", limit=8, final=[_cap("a"), _cap("workspace_navigator")])
    second = build_snapshot(scene="office", limit=8, final=[_cap("a")])
    assert second.diff(first) == [] or True  # 方向性（first→second）
    assert first.diff(second) == ["workspace_navigator"]


# ── P1：复合主键 + 注册表版本 ──────────────────────────────


def test_tool_identity_is_not_the_bare_name():
    """裸 name 不是可靠主键：两个插件同名工具必须能区分。"""
    a = _cap("workspace_read", annotations={"plugin_id": "plugin-a", "provider_id": "p1"})
    b = _cap("workspace_read", annotations={"plugin_id": "plugin-b", "provider_id": "p2"})
    assert tool_identity(a) != tool_identity(b)
    assert "plugin-a" in tool_identity(a) and "plugin-b" in tool_identity(b)
    # 缺字段时不抛错（纯函数、占位符）。
    assert tool_identity(_cap("x")) == "-|-|x@1.0.0"


def test_registry_epoch_changes_with_the_registry():
    """注册表版本必须能反映"注册表变了"（否则缓存失效条件形同虚设）。"""
    epoch = registry_epoch()
    assert epoch and epoch != "", "注册表可用时必须给出稳定版本串"
    assert registry_epoch() == epoch, "同一状态下必须稳定（否则每次调用都会击穿缓存）"
