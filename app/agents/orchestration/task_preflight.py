"""通用任务入口前置检查。

This module deliberately does not identify business domains.  It only guards
the expensive execution path when a request clearly asks for an external
side-effect but gives neither an operation target nor an authorized context.
Pure generation and read/analysis requests are never blocked here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


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


@dataclass(frozen=True, slots=True)
class TaskPreflight:
    """入口检查结果；调用方可直接把 question 作为终态澄清返回。"""

    needs_clarification: bool = False
    question: str = ""
    reason: str = ""


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
    """

    text = str(request or "").strip()
    effect = bool(_EFFECT_RE.search(text))
    # Creation is ambiguous: “创建一份报告” is pure generation, while
    # “创建文件/项目/工作区” changes an external resource.  Only the latter
    # enters this preflight gate.
    if not effect and not (_CREATE_RE.search(text) and _LOCAL_TARGET_RE.search(text)):
        return TaskPreflight()

    if _BARE_EFFECT_RE.match(text):
        return TaskPreflight(
            needs_clarification=True,
            reason="effect_without_target",
            question="请说明希望我处理什么内容或对象，以及期望的结果。",
        )

    has_context = bool(str(workspace_id or project_id or "").strip()) or any(
        isinstance(item, dict) and str(item.get("doc_id") or "").strip()
        for item in (office_docs or [])
    ) or bool(_CONTEXT_RE.search(text))
    has_target = bool(_TARGET_RE.search(text))

    # A trusted context can be inspected by an Agent, but an absent target is
    # still unsafe for mutation.  Ask once at the boundary instead of allowing
    # repeated list/read calls to discover the same missing information.
    if not has_target:
        return TaskPreflight(
            needs_clarification=True,
            reason="effect_without_target",
            question=(
                "这个请求包含外部操作，但还缺少操作对象。请说明要处理的对象或范围，"
                "以及希望新建、修改、覆盖、删除还是执行；如果涉及本地内容，请同时选择或创建工作区。"
            ),
        )

    # A local-looking target without an authorized scope must not cause the
    # planner to guess a filesystem path.  The model can still answer with a
    # draft when the user only wants generated content; this gate is reached
    # only for effect verbs above.
    if not has_context and (
        _LOCAL_TARGET_RE.search(text)
        or _PATH_RE.search(text)
    ):
        return TaskPreflight(
            needs_clarification=True,
            reason="local_effect_without_scope",
            question=(
                "请先选择或创建要操作的工作区，并说明目标路径及新建、修改或覆盖方式。"
            ),
        )

    return TaskPreflight()


__all__ = ["TaskPreflight", "preflight_external_effect"]
