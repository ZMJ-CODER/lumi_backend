"""两代能力对照 · 缺口 3 **批次 4**：SSE 事件 / 过程日志 / Provider 标签的跨代守卫。

批次划分见 `docs/CAPABILITY_TWO_GENERATIONS.md` §3.5。这一批守的是**出口**：
前端看到的过程条目与事件流。三条性质：

1. **标签是目录事实，与开关无关**：同一工具在 `TOOL_REGISTRY_DERIVED` 两种状态下
   给出同一份标签；`provider_id` 出现时，必须真的在"可用 Provider"集合里
   （缺口 2a 之后这条意味着"只有声明"的 Provider 永远不会出现在出口上）；
2. **出口只放闭集词汇**：形状闸门（无空格/斜杠/引号/超长）由机器保证，
   参数、路径与用户输入不可能伪装成标签；
3. **历史载荷兼容**：老过程条目（没有标签字段）逐字不变，新字段默认不出现——
   这是后来改 provider id（缺口 2c）不破历史回放的前提。
"""

from __future__ import annotations

import json

import pytest
from lumi_contracts.events.process import (
    ProcessLogEntry,
    label_value,
    sanitize_process_text,
)
from lumi_contracts.plugins import capability_ok

from app.agents.capabilities.broker.resource_dispatch import (
    adapter_snapshot,
    dispatch_labels,
    provider_ids_for,
)
from app.agents.capabilities.catalog.resource import PROVIDERS_BY_NAME, TOOL_BINDINGS

#: 出口允许出现的标签字段（**闭集**；新增字段要同时改契约与前端）。
_LABEL_KEYS = {
    "capability",
    "resource_type",
    "provider_id",
    "provider_name",
    "provider_kind",
    "provider_label",
    "display_name",
}


def _registered_provider_ids() -> set[str]:
    return {row["provider_id"] for row in adapter_snapshot() if row["registered"] and row["provider_id"]}


# ── 1. 标签是目录事实 ────────────────────────────────────────


@pytest.mark.parametrize("derived", [False, True])
def test_labels_are_catalog_facts_independent_of_the_flag(monkeypatch, derived):
    from app.core.config import settings

    monkeypatch.setattr(settings, "TOOL_REGISTRY_DERIVED", derived)
    for tool in sorted(TOOL_BINDINGS):
        labels = dispatch_labels(tool)
        assert set(labels) <= _LABEL_KEYS, (tool, labels)
        for key, value in labels.items():
            if key == "provider_label":
                # 展示文案来自**固定表**（不是用户输入），走文案闸门而不是词汇闸门
                assert sanitize_process_text(value, limit=32) == value, (tool, value)
                continue
            assert label_value(value) == value, (tool, key, value)
        if labels.get("provider_name"):
            assert labels["provider_name"] in PROVIDERS_BY_NAME, tool
        if labels.get("provider_id"):
            # 出口上的 provider_id 必须真的可用（只有声明、没有实现的不许出现）
            assert labels["provider_id"] in _registered_provider_ids(), (tool, labels)
            assert labels["provider_id"] in provider_ids_for(
                labels["capability"], labels["resource_type"]
            ), (tool, labels)


def test_tools_without_a_resource_binding_export_no_labels():
    """本机动作/编排原语不接资源层 ⇒ 出口上什么都不填（"不认识"≠"编一个"）。"""
    for tool in ("desktop_open_app", "user_clarify", "AskUserQuestion", "totally_unknown_tool"):
        assert dispatch_labels(tool) == {}, tool


def test_labels_never_carry_parameters_paths_or_user_input():
    """形状闸门必须在**出口**生效：带参数/路径/引号的名字一律丢弃。"""
    for name in (
        "workspace_write --path=/home/u/.env",
        "workspace_write/../../etc/passwd",
        "workspace_write'; DROP TABLE jobs;--",
        'workspace_write"',
        "workspace write",
        "x" * 200,
        "",
    ):
        assert dispatch_labels(name) == {}, name
        assert label_value(name) is None, name
    # 合法的闭集词汇不能被误杀
    for value in ("resource.write", "workspace", "lumi.local.workspace", "workspace_provider", "Read"):
        assert label_value(value) == value


# ── 2. 过程条目：闭集 + 老载荷逐字不变 ───────────────────────


def test_process_entry_drops_junk_labels_and_keeps_old_payload_shape():
    from app.contracts.process_log import process_log_payload

    bare = ProcessLogEntry(id="e1", title="读文件", summary="ok")
    payload = process_log_payload([bare])[0]
    assert not (_LABEL_KEYS & set(payload)), payload

    polluted = ProcessLogEntry(
        id="e2",
        title="读文件",
        capability="workspace_read --path=/etc/passwd",
        resource_type="workspace/../..",
        provider_id="lumi.local.workspace",
        provider_name="not a provider name",
        display_name="Read",
    )
    assert polluted.capability is None
    assert polluted.resource_type is None
    assert polluted.provider_name is None
    assert polluted.provider_id == "lumi.local.workspace"
    assert polluted.display_name == "Read"


def test_process_projection_and_catalog_agree():
    """实时投影与目录派生必须给出同一份标签（否则刷新前后两行不一样）。"""
    from app.contracts.process_log import dispatch_labels_for_step

    for tool in sorted(TOOL_BINDINGS):
        assert dispatch_labels_for_step({}, tool) == dispatch_labels(tool), tool


def test_step_persisted_labels_win_over_derivation():
    """步骤里已经落盘的标签优先（调用方更清楚"模型当时叫什么"）。"""
    from app.contracts.process_log import dispatch_labels_for_step

    step = {
        "capability": "resource.read",
        "resource_type": "workspace",
        "display_name": "Read",
    }
    labels = dispatch_labels_for_step(step, "workspace_navigator")
    assert labels["capability"] == "resource.read"
    assert labels["display_name"] == "Read"


# ── 3. 能力事件：执行来源在，正文不在 ────────────────────────


def test_capability_event_carries_execution_source_but_never_the_payload():
    from app.contracts.events import SseEventEncoder
    from app.services.capability_events import events_for_result

    result = capability_ok(
        {
            "status": "ok",
            "content": "SECRET sk-abc123",
            "path": "C:\\Users\\me\\.env",
        },
        capability="workspace.read@1",
        provider_id="lumi.local.workspace",
        execution_plane="client",
        runtime_kind="worker",
        served_locally=True,
    )
    event = events_for_result(result, capability="workspace.read@1", job_id="job-1")
    frame = SseEventEncoder(job_id="job-1").frame(event)

    assert frame["type"] == "capability_completed"
    assert frame["capability"] == "workspace.read@1"
    assert frame["provider_id"] == "lumi.local.workspace"
    assert frame["execution_plane"] == "client"
    assert frame["executor_type"], "前端不必从 deployment 猜谁在执行"

    dumped = json.dumps(frame, ensure_ascii=False)
    assert "SECRET" not in dumped
    assert "sk-abc123" not in dumped
    assert "Users" not in dumped


# ── 4. 单调性：新增工具不改变既有出口 ────────────────────────


def test_adding_a_tool_does_not_change_existing_labels():
    from app.agents.capabilities.catalog.tool_registry import invalidate_cache
    from app.agents.skills.base import Tool, ToolOutput
    from app.agents.skills.registry import ToolRegistry

    before = {tool: dispatch_labels(tool) for tool in sorted(TOOL_BINDINGS)}
    probe = "demo_label_probe"
    assert probe not in before

    class _Probe(Tool):
        description = "测试用：新增工具不能改变既有标签"
        category = "devtools"
        environment = "client"
        capability = "resource.read"
        resource_type = "memory"
        parameters_schema = {"type": "object", "properties": {}}

        async def execute(self, params, context=None):  # noqa: ANN001
            return ToolOutput(success=True, output="ok", data=dict(params))

    _Probe.name = probe
    ToolRegistry.register(_Probe(), source="test")
    try:
        after = {tool: dispatch_labels(tool) for tool in sorted(TOOL_BINDINGS)}
        assert after == before
        # 新工具本身：能力/资源类型来自声明，但 provider_id 仍为空（只有声明）
        added = dispatch_labels(probe)
        assert added["capability"] == "resource.read"
        assert added["resource_type"] == "memory"
        assert "provider_id" not in added, added
    finally:
        ToolRegistry.unregister(probe)
        invalidate_cache()
