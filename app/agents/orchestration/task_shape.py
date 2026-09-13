"""Generic task-shape gate for the office entry point.

The gate intentionally does not classify business domains.  It only answers
one question: can the model answer from supplied conversation/context, or does
the request require an external capability, state transition, or a runtime
decision loop?

**方案 4 定位（重要）**：本模块已降级为**纯适配器**，不再充当业务路由依据。

* 单一意图事实源是 ``task_assessor`` 产出的 ``TaskProfile``；
* 本模块的用途只剩两个：
  1. 把已有的 ``TaskProfile`` 投影成旧 ``TaskShape``（:func:`shape_from_profile`），
     供还没切到画像的旧调用方读取；
  2. 兼容兜底：完全没有画像时（``TASK_PROFILE_CANONICAL`` 关闭且没有 Router v2
     决策）用正则给出一个保守的"要不要编排"，**仅供影子比对与旧路径**。
* 正则在 Phase 6（全面切换后）删除；安全层的危险命令/越权规则不在此列。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class TaskShape:
    requires_orchestration: bool
    reasons: tuple[str, ...] = ()
    #: 判定来源（``profile`` = 画像投影；``legacy_regex`` = 兜底词表）。
    source: str = "legacy_regex"


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
    """**兼容兜底**判定（正则词表）：只在没有画像可用时作为影子比对的旧侧输入。

    方案 4 起业务路由以 ``TaskProfile`` 为准；本函数的结论**不得**用于新逻辑的
    路由/工具注入决策（新代码请用 :func:`shape_from_profile`）。
    """
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
    return TaskShape(bool(reasons), tuple(dict.fromkeys(reasons)), source="legacy_regex")


#: 动作意图 → 旧 reasons 词表（唯一映射；不新增语义）。
_ACTION_REASONS: dict[str, str] = {
    "CREATE": "side_effect",
    "MODIFY": "side_effect",
    "DELETE": "side_effect",
    "MOVE": "side_effect",
    "SEND": "side_effect",
    "PUBLISH": "side_effect",
    "EXECUTE": "side_effect",
    "READ": "external_capability",
    "SEARCH": "external_capability",
}


def shape_from_profile(profile: Any = None, *, context_chars: int = 0, context_budget: int = 24000) -> TaskShape:
    """``TaskProfile``（契约或内核画像）→ 旧 ``TaskShape``（**纯适配器**）。

    只做字段投影，不做任何文本解析：动作意图非空 ⇒ 需要编排；否则再按复杂度/
    运行时决策判断。画像缺失时返回"不需要编排"的保守结论，由调用方决定是否退回
    :func:`assess_task_shape`。
    """
    intents = _profile_intents(profile)
    reasons: list[str] = []
    for intent in intents:
        reason = _ACTION_REASONS.get(intent)
        if reason and reason not in reasons:
            reasons.append(reason)
    complexity = str(
        getattr(profile, "complexity", "") if profile is not None
        else ""
    ).upper()
    if complexity in {"M2", "SEQUENTIAL"}:
        reasons.append("dependency")
    elif complexity in {"M3", "DYNAMIC"}:
        reasons.append("runtime_decision")
    if bool(_profile_field(profile, "has_dependency")):
        reasons.append("dependency")
    if bool(_profile_field(profile, "has_runtime_decision")):
        reasons.append("runtime_decision")
    if context_chars > max(1, int(context_budget)):
        reasons.append("context_budget")
    unique = tuple(dict.fromkeys(reasons))
    return TaskShape(bool(unique), unique, source="profile")


def _profile_intents(profile: Any) -> tuple[str, ...]:
    if profile is None:
        return ()
    values = _profile_field(profile, "action_intents") or ()
    return tuple(
        str(getattr(item, "value", item)).strip().upper() for item in values if str(item).strip()
    )


def _profile_field(profile: Any, key: str, default: Any = None) -> Any:
    if profile is None:
        return default
    if isinstance(profile, dict):
        return profile.get(key, default)
    return getattr(profile, key, default)


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
            return TaskShape(True, (*base.reasons, "registered_skill"), source=base.source)
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
                return TaskShape(True, (*base.reasons, "registered_skill"), source=base.source)
    except Exception:
        pass
    return base


__all__ = [
    "TaskShape",
    "assess_task_shape",
    "assess_task_shape_with_skills",
    "shape_from_profile",
]
