"""Generic task-shape gate for the office entry point.

The gate intentionally does not classify business domains.  It only answers
one question: can the model answer from supplied conversation/context, or does
the request require an external capability, state transition, or a runtime
decision loop?
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TaskShape:
    requires_orchestration: bool
    reasons: tuple[str, ...] = ()


_SIDE_EFFECT = re.compile(
    r"(?iu)(?:创建|新增|修改|编辑|删除|替换|写入|保存|导出|发送|提交|审批|执行|运行|跑起来|启动|打开|安装|部署|发布|重命名|移动|复制|create|update|delete|write|save|send|submit|approve|run|execute|open|install|deploy)"
)
_EXTERNAL_CAPABILITY = re.compile(
    r"(?iu)(?:联网|网页|网上|公开资料|外部资料|最新|实时|当前天气|股价|汇率|新闻|搜索|检索|调研|研究|政策|竞品|知识库|数据库|系统状态|应用|接口|api|url|邮件|日历|日程|待办|进程|命令|脚本|shell|bash|powershell|web|search|browse|查一下(?:.{0,8}(?:资料|信息|政策|新闻|价格|竞品)))"
)
_DEPENDENCY = re.compile(
    r"(?iu)(?:然后|之后|再|最后|先.+再|根据.+结果|如果.+就|直到|循环|逐个|批量|分别|并行|依赖|多步骤|first.+then|after|based on|if.+then)"
)
_RUNTIME_DECISION = re.compile(
    r"(?iu)(?:自行判断|自行决定|遇到问题|失败后|不行就|直到成功|确保通过|验证|测试|排查|诊断|修复|定位|根据情况|动态|按结果)"
)


def assess_task_shape(request: str, *, context_chars: int = 0, context_budget: int = 24000) -> TaskShape:
    text = str(request or "").strip()
    reasons: list[str] = []
    if _SIDE_EFFECT.search(text):
        reasons.append("side_effect")
    if _EXTERNAL_CAPABILITY.search(text):
        reasons.append("external_capability")
    if _DEPENDENCY.search(text):
        reasons.append("dependency")
    if _RUNTIME_DECISION.search(text):
        reasons.append("runtime_decision")
    if context_chars > max(1, int(context_budget)):
        reasons.append("context_budget")
    return TaskShape(bool(reasons), tuple(dict.fromkeys(reasons)))


async def assess_task_shape_with_skills(
    request: str,
    *,
    context_chars: int = 0,
    context_budget: int = 24000,
    user_id: str = "",
    scene: str = "office",
) -> TaskShape:
    """Additive skill signal for user-installed Prompt-as-Code Skills.

    A Skill can extend the system without changing this gate.  Only a decisive
    workflow Skill is considered; generic text Skills remain direct model work.
    """
    base = assess_task_shape(request, context_chars=context_chars, context_budget=context_budget)
    if base.requires_orchestration:
        return base
    try:
        from app.agents.skills.selection import select_workflow_skill

        skill = select_workflow_skill(request, scene=scene, user_id=user_id)
        if skill is not None and (
            bool(getattr(skill, "write_op", False))
            or str(getattr(skill, "safety_level", "READ_ONLY") or "READ_ONLY").upper() != "READ_ONLY"
        ):
            return TaskShape(True, (*base.reasons, "registered_skill"))
        # Do not promote every read-only Skill merely because it declares
        # tools/sources.  Generic explanations and summaries must stay on the
        # direct path; a Skill is an orchestration signal only when its own
        # metadata is decisively matched by the request above.
    except Exception:
        pass
    # User-created Prompt-as-Code Skills are loaded from persistence rather
    # than the builtin registry.  Their metadata is the extension point: a
    # matching use_when/intent phrase or any declared external source makes
    # the request an orchestration candidate without adding a new route.
    try:
        from app.services.user_workflow_skills import get_visible_workflow_skills

        lowered = str(request or "").casefold()
        for skill in await get_visible_workflow_skills(user_id):
            if not skill.supports_scene(scene) or getattr(skill, "status", "stable") != "stable":
                continue
            phrases = [
                *list(getattr(skill, "intent_tags", None) or []),
                *list(getattr(skill, "use_when", None) or []),
            ]
            phrase_match = any(str(item).strip().casefold() in lowered for item in phrases if str(item).strip())
            declared_external = bool(
                getattr(skill, "write_op", False)
                or getattr(skill, "allowed_tools", None)
                or set(getattr(skill, "provided_sources", None) or []) - {"USER_INPUT"}
            )
            if phrase_match and declared_external:
                return TaskShape(True, (*base.reasons, "registered_skill"))
    except Exception:
        pass
    return base
