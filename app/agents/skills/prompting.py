"""Compile model-facing Skill selection policy from registry metadata."""

from __future__ import annotations

from collections.abc import Iterable

from app.agents.skills.capability import ToolCapability


def build_tool_selection_contract(capabilities: Iterable[ToolCapability]) -> str:
    """Return a compact policy fragment derived from the injected candidates.

    This is deliberately generated from the registry contract instead of a
    second hand-written tool matrix. It contains no user text or secrets.
    """
    lines = [
        "候选工具选择边界（由注册契约生成）：",
        "只在当前候选工具确实满足目标时调用；不要为了试探而调用。",
    ]
    for capability in capabilities:
        lines.append(f"- {capability.name}@{capability.version}：")
        if capability.use_when:
            lines.append("  适用：" + "；".join(capability.use_when[:2]))
        if capability.do_not_use_when:
            lines.append("  不适用：" + "；".join(capability.do_not_use_when[:2]))
        if capability.handoff_to:
            lines.append("  下一步交接：" + ", ".join(capability.handoff_to[:3]))
    return "\n".join(lines)
