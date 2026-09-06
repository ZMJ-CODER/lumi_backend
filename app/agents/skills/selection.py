"""Declarative Workflow Skill selection.

This module deliberately knows nothing about individual business requests or
tool names.  It ranks the registered Skill metadata (tags, use/do-not-use
guidance and description) and returns a Skill only when the metadata provides
enough evidence.  Atomic tools remain the fallback for one-step facts.
"""

from __future__ import annotations

import re

from app.agents.skills.base import WorkflowSkill
from app.agents.skills.registry import SkillRegistry


def _terms(value: str) -> set[str]:
    text = str(value or "").casefold()
    terms = set(re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", text))
    han = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    terms.update(han[i : i + 2] for i in range(max(0, len(han) - 1)))
    return {item for item in terms if item}


def _skill_terms(skill: WorkflowSkill) -> set[str]:
    return _terms(" ".join([
        skill.name,
        skill.description,
        " ".join(getattr(skill, "intent_tags", []) or []),
        " ".join(getattr(skill, "use_when", []) or []),
    ]))


def _phrase_score(request: str, skill: WorkflowSkill) -> int:
    """Reward complete intent phrases, which are more discriminative than
    generic words such as ``资料`` or ``问题`` shared by many Skills."""
    text = str(request or "").casefold()
    score = 0
    phrases = list(getattr(skill, "intent_tags", []) or []) + list(getattr(skill, "use_when", []) or [])
    for phrase in phrases:
        value = str(phrase or "").strip().casefold()
        if len(value) >= 2 and value in text:
            score += 3 if len(value) >= 4 else 1
    return score


def select_workflow_skill(
    request: str,
    *,
    scene: str = "office",
    user_id: str = "",
    min_score: int = 2,
) -> WorkflowSkill | None:
    """Return the best visible workflow Skill when metadata is decisive.

    ``do_not_use_when`` is treated as a negative signal, while public Skills
    and the current user's private Skills are both considered through the
    registry visibility API.  A close tie is rejected so a new Skill cannot
    silently change an existing route.
    """
    query_terms = _terms(request)
    if not query_terms:
        return None
    # A single current fact (weather, price, exchange rate, news headline)
    # should remain an atomic read.  Multi-source comparison/research is what
    # the information-research Workflow owns.  This keeps the generic helper
    # metadata-driven while avoiding a workflow wrapper around trivial calls.
    freshness_only = bool(re.search(
        r"(?iu)(天气|气温|降雨|股价|汇率|行情|新闻).{0,12}(今天|现在|当前|最新|实时)|"
        r"(今天|现在|当前|最新|实时).{0,12}(天气|气温|降雨|股价|汇率|行情|新闻)",
        request,
    ))
    multi_source_hint = bool(re.search(
        r"(?iu)(查资料|查信息|检索|调研|研究|比较|对比|综述|官方资料|官方文档|文献|多个来源|各自适合|适合什么场景)",
        request,
    ))
    candidates: list[tuple[int, str, WorkflowSkill]] = []
    for skill in SkillRegistry.list_visible(user_id):
        if skill.status != "stable" or not skill.supports_scene(scene):
            continue
        if freshness_only and not multi_source_hint:
            continue
        positive = len(query_terms & _skill_terms(skill)) + _phrase_score(request, skill)
        negative = len(query_terms & _terms(" ".join(getattr(skill, "do_not_use_when", []) or [])))
        score = positive - negative
        if score >= min_score:
            candidates.append((score, skill.name, skill))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    if not candidates:
        return None
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None
    return candidates[0][2]
