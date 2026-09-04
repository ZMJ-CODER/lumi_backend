"""开发者 Workflow Skill 的选择契约静态完整性检查。"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from app.agents.skills.base import WorkflowSkill


def lint_skill_contracts(skills: Iterable[WorkflowSkill]) -> list[str]:
    """Return deterministic errors; callers decide whether reload may proceed."""
    items = list(skills)
    names = {skill.name for skill in items}
    errors: list[str] = []
    for skill in items:
        for target in [
            *getattr(skill, "handoff_to", []),
            *getattr(skill, "conflicts_with", []),
            *getattr(skill, "preferred_over", []),
        ]:
            if target not in names:
                errors.append(f"{skill.name}: 引用的 Skill 不存在: {target}")
        if getattr(skill, "bootstrap_until", ""):
            if not getattr(skill, "bootstrap_intents", []):
                errors.append(f"{skill.name}: bootstrap_until 需要 bootstrap_intents，不能全量强制入池")
            else:
                try:
                    date.fromisoformat(skill.bootstrap_until)
                except ValueError:
                    errors.append(f"{skill.name}: bootstrap_until 必须是 YYYY-MM-DD")

    # High lexical overlap between different stable tools needs an explicit
    # relationship. This is intentionally conservative: only exact tag
    # overlap of two or more tags is a registration failure.
    for index, left in enumerate(items):
        left_tags = {tag.casefold() for tag in getattr(left, "intent_tags", []) if tag}
        for right in items[index + 1:]:
            overlap = left_tags & {tag.casefold() for tag in getattr(right, "intent_tags", []) if tag}
            if len(overlap) < 2:
                continue
            related = right.name in {
                *getattr(left, "handoff_to", []), *getattr(left, "conflicts_with", []), *getattr(left, "preferred_over", [])
            }
            related = related or left.name in {
                *getattr(right, "handoff_to", []), *getattr(right, "conflicts_with", []), *getattr(right, "preferred_over", [])
            }
            if not related:
                errors.append(
                    f"{left.name}/{right.name}: intent_tags 重叠 {sorted(overlap)}，"
                    "需声明 handoff_to、conflicts_with 或 preferred_over"
                )
    return errors
