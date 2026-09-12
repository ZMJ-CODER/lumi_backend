"""通用任务入口前置检查。

This module deliberately does not identify business domains.  It only guards
the expensive execution path when a request clearly asks for an external
side-effect but gives neither an operation target nor an authorized context.
Pure generation and read/analysis requests are never blocked here.

灰度（``CAPABILITY_PREFLIGHT_V2``）：

* **关闭（默认）**：完全走本模块的历史判定，行为逐字不变；
* **打开**：``preflight_external_effect`` 内部改由
  :class:`~app.agents.orchestration.capability_preflight_service.CapabilityPreflightService`
  做判定（唯一预检入口），本模块只负责把服务结论翻回既有的
  ``needs_clarification`` / ``question`` / ``reason`` 语义。
  因此对外可观察结果与关闭时一致，但"谁做判定"已经收口到服务。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.agents.orchestration.capability_preflight import PreflightStatus

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注（运行期惰性导入避免循环）
    from app.agents.orchestration.capability_preflight_service import (
        CapabilityPreflightService,
    )

_EFFECT_RE = re.compile(
    r"(?iu)(?:写入|保存|修改|编辑|改动|改一下|删除|覆盖|替换|发送|提交|审批|执行|运行|启动|安装|部署|发布|重命名|移动|复制|"
    r"write|save|edit|modify|delete|overwrite|replace|send|submit|approve|execute|run|install|deploy|publish|rename|move|copy)"
)
_CREATE_RE = re.compile(r"(?iu)(?:创建|新建|create|new)")
_LOCAL_TARGET_RE = re.compile(
    r"(?iu)(?:文件|目录|文件夹|工作区|项目|路径|workspace|project|file|folder|path)"
)
_PATH_RE = re.compile(r"(?iu)(?:^|[\s'\"`])(?:\.?\.?[\\/])?[\w\u4e00-\u9fff.-]+(?:[\\/][\w\u4e00-\u9fff.-]+)+|[\w.-]+\.[A-Za-z0-9]{1,8}")
_TARGET_RE = re.compile(
    r"(?iu)(?:文件|目录|文件夹|工作区|项目|文档|表格|邮件|日历|日程|记录|任务|应用|进程|数据库|接口|网页|链接|"
    r"目标|对象|路径|名称|编号|id|[A-Za-z0-9_./\\-]+\.[A-Za-z0-9]{1,8})"
)
_CONTEXT_RE = re.compile(
    r"(?iu)(?:当前工作区|已选择(?:的)?工作区|已授权(?:的)?工作区|当前项目|已选择(?:的)?项目|"
    r"上传的|附件|这份|该文件|本地已有|服务器上的|数据库中的|系统中的|我的账户|my workspace|"
    r"selected workspace|authorized workspace|current project|selected project|attachment|"
    r"local existing|server|database)"
)
_BARE_EFFECT_RE = re.compile(
    r"(?iu)^(?:帮我|请|麻烦)?\s*(?:写入|保存|修改|编辑|删除|覆盖|替换|发送|提交|审批|执行|运行|启动|安装|部署|发布|重命名|移动|复制|创建|新建)"
    r"(?:一下|下|这个|那个|它)?\s*[。！!？?]*$"
)

#: 本模块只回答"是否请求了外部副作用"，不解析具体动作类型。
#: 动作意图 → 工具窗口的**唯一**映射在 ``capability_preflight.ACTION_TOOL_WINDOW``；
#: 这里的标记刻意不在映射表内：本入口不做工具窗口判定（工具注入在计划编译之后）。
_EFFECT_INTENT = "EXTERNAL_EFFECT"

_REASON_EFFECT_WITHOUT_TARGET = "effect_without_target"
_REASON_LOCAL_EFFECT_WITHOUT_SCOPE = "local_effect_without_scope"

_QUESTION_BARE_EFFECT = "请说明希望我处理什么内容或对象，以及期望的结果。"
_QUESTION_EFFECT_WITHOUT_TARGET = (
    "这个请求包含外部操作，但还缺少操作对象。请说明要处理的对象或范围，"
    "以及希望新建、修改、覆盖、删除还是执行；如果涉及本地内容，请同时选择或创建工作区。"
)
_QUESTION_LOCAL_EFFECT_WITHOUT_SCOPE = (
    "请先选择或创建要操作的工作区，并说明目标路径及新建、修改或覆盖方式。"
)

#: 服务状态 → 本入口的澄清原因（只覆盖本入口能表达的两种澄清语义）。
_CLARIFICATION_REASON_BY_STATUS: dict[str, str] = {
    PreflightStatus.NEEDS_CLARIFICATION.value: _REASON_EFFECT_WITHOUT_TARGET,
    PreflightStatus.DEPENDENCY_MISSING.value: _REASON_LOCAL_EFFECT_WITHOUT_SCOPE,
}


@dataclass(frozen=True, slots=True)
class TaskPreflight:
    """入口检查结果；调用方可直接把 question 作为终态澄清返回。"""

    needs_clarification: bool = False
    question: str = ""
    reason: str = ""


def has_external_effect(request: str) -> bool:
    """请求是否明确要求外部副作用（与历史判定同一组正则）。"""
    text = str(request or "").strip()
    effect = bool(_EFFECT_RE.search(text))
    # Creation is ambiguous: “创建一份报告” is pure generation, while
    # “创建文件/项目/工作区” changes an external resource.  Only the latter
    # enters this preflight gate.
    return effect or bool(_CREATE_RE.search(text) and _LOCAL_TARGET_RE.search(text))


def requires_local_scope(request: str) -> bool:
    """请求是否指向本地目标（本地目标/路径）——需要已授权范围。"""
    text = str(request or "")
    return bool(_LOCAL_TARGET_RE.search(text) or _PATH_RE.search(text))


def has_trusted_context(
    request: str,
    *,
    workspace_id: str | None = None,
    project_id: str | None = None,
    office_docs: list[dict] | None = None,
) -> bool:
    """是否已有可信上下文（绑定工作区/项目、已授权附件，或明确的上下文指代）。"""
    return (
        bool(str(workspace_id or project_id or "").strip())
        or any(
            isinstance(item, dict) and str(item.get("doc_id") or "").strip()
            for item in (office_docs or [])
        )
        or bool(_CONTEXT_RE.search(str(request or "")))
    )


def clarification_for_request(
    request: str,
    *,
    workspace_id: str | None = None,
    project_id: str | None = None,
    office_docs: list[dict] | None = None,
) -> TaskPreflight:
    """历史判定本身（纯文本启发式）。

    它只回答"这个副作用请求缺什么信息"，因此 V2 打开时也被复用来生成对用户
    的提问文案（判定由服务给出，文案保持兼容）。
    """
    text = str(request or "").strip()
    if not has_external_effect(text):
        return TaskPreflight()

    if _BARE_EFFECT_RE.match(text):
        return TaskPreflight(
            needs_clarification=True,
            reason=_REASON_EFFECT_WITHOUT_TARGET,
            question=_QUESTION_BARE_EFFECT,
        )

    has_context = has_trusted_context(
        text, workspace_id=workspace_id, project_id=project_id, office_docs=office_docs
    )
    has_target = bool(_TARGET_RE.search(text))

    # A trusted context can be inspected by an Agent, but an absent target is
    # still unsafe for mutation.  Ask once at the boundary instead of allowing
    # repeated list/read calls to discover the same missing information.
    if not has_target:
        return TaskPreflight(
            needs_clarification=True,
            reason=_REASON_EFFECT_WITHOUT_TARGET,
            question=_QUESTION_EFFECT_WITHOUT_TARGET,
        )

    # A local-looking target without an authorized scope must not cause the
    # planner to guess a filesystem path.  The model can still answer with a
    # draft when the user only wants generated content; this gate is reached
    # only for effect verbs above.
    if not has_context and requires_local_scope(text):
        return TaskPreflight(
            needs_clarification=True,
            reason=_REASON_LOCAL_EFFECT_WITHOUT_SCOPE,
            question=_QUESTION_LOCAL_EFFECT_WITHOUT_SCOPE,
        )

    return TaskPreflight()


def capability_preflight_facts(
    request: str,
    *,
    workspace_id: str | None = None,
    project_id: str | None = None,
    office_docs: list[dict] | None = None,
) -> dict:
    """请求 → ``CapabilityPreflightService`` 所需的服务端**事实**（不猜业务意图）。

    ``profile`` / ``workspace_bound`` / ``requires_workspace`` 三项与
    :func:`clarification_for_request` 使用同一组判定条件，所以 V2 打开时服务可以
    完全取代历史澄清门，且结论一致。
    """
    text = str(request or "").strip()
    effect = has_external_effect(text)
    return {
        "profile": {
            "intent_type": "EXECUTE_ACTION" if effect else "GENERATE_ONLY",
            "action_intents": [_EFFECT_INTENT] if effect else [],
            "target_clarity": "KNOWN" if _TARGET_RE.search(text) else "UNKNOWN",
        },
        "workspace_bound": has_trusted_context(
            text, workspace_id=workspace_id, project_id=project_id, office_docs=office_docs
        ),
        "requires_workspace": requires_local_scope(text),
    }


def preflight_external_effect(
    request: str,
    *,
    workspace_id: str | None = None,
    project_id: str | None = None,
    office_docs: list[dict] | None = None,
) -> TaskPreflight:
    """Check only high-confidence missing execution context.

    The check is intentionally conservative: it does not decide which Skill or
    Tool is needed.  It merely avoids starting a long planner/ReAct loop when a
    side-effect request has no identifiable target and no trusted context.

    兼容入口：开关关闭时行为与改造前逐字一致；打开时判定改由
    ``CapabilityPreflightService`` 给出（见模块 docstring）。
    """
    from app.agents.orchestration.capability_preflight_service import (
        CapabilityPreflightService,
    )

    service = CapabilityPreflightService()
    if not service.enabled():
        return clarification_for_request(
            request,
            workspace_id=workspace_id,
            project_id=project_id,
            office_docs=office_docs,
        )
    return _service_preflight(
        service,
        request,
        workspace_id=workspace_id,
        project_id=project_id,
        office_docs=office_docs,
    )


def _service_preflight(
    service: "CapabilityPreflightService",
    request: str,
    *,
    workspace_id: str | None = None,
    project_id: str | None = None,
    office_docs: list[dict] | None = None,
) -> TaskPreflight:
    """V2：由服务判定，再把结论翻回本入口的澄清语义（不改用户可见结果）。"""
    facts = capability_preflight_facts(
        request, workspace_id=workspace_id, project_id=project_id, office_docs=office_docs
    )
    if not facts["profile"]["action_intents"]:
        # 纯生成/只读请求不进预检门（与历史判定一致）。
        return TaskPreflight()
    result = service.preflight(
        profile=facts["profile"],
        # 本入口没有底层探测事实（Probe 由 Orchestrator 注入）；没有事实就不猜。
        probe=None,
        workspace_bound=bool(facts["workspace_bound"]),
        requires_workspace=bool(facts["requires_workspace"]),
    )
    if result.ok:
        return TaskPreflight()
    reason = _CLARIFICATION_REASON_BY_STATUS.get(result.status)
    if reason is None:
        # 其余状态（能力不可用/权限等）在这里没有表达能力：留给 Orchestrator 的
        # 预检门处理，本入口保持"不阻断"。
        return TaskPreflight()
    wording = clarification_for_request(
        request, workspace_id=workspace_id, project_id=project_id, office_docs=office_docs
    )
    return TaskPreflight(
        needs_clarification=True,
        reason=wording.reason or reason,
        question=wording.question or result.question,
    )


__all__ = [
    "TaskPreflight",
    "capability_preflight_facts",
    "clarification_for_request",
    "has_external_effect",
    "has_trusted_context",
    "preflight_external_effect",
    "requires_local_scope",
]
