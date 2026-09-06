"""依据注册表元数据编译面向模型的 Skill 选择策略。"""

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
        "候选工具语义接近时，依据用户目标、数据来源、动作类型和 use_when/do_not_use_when 在内部选择；"
        "不得把候选冲突、分数或路由实现细节告诉用户。只有缺少必填参数或授权时才请求澄清。",
        "调用工具时必须填写 JSON Schema 的全部 required 字段。对于 query、expression、name 等文本参数，"
        "保留用户给出的核心实体、限定词和表达式；不要翻译、扩写或凭空补充条件。",
    ]
    for capability in capabilities:
        lines.append(f"- {capability.name}@{capability.version}：")
        if capability.use_when:
            lines.append("  适用：" + "；".join(capability.use_when[:2]))
        if capability.do_not_use_when:
            lines.append("  绝对禁止：" + "；".join(capability.do_not_use_when[:3]))
        if capability.handoff_to:
            lines.append("  下一步交接：" + ", ".join(capability.handoff_to[:3]))
    return "\n".join(lines)
