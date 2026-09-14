"""Step 完成展示文案：实时帧与刷新投影**同源**的那套措辞。

``app/contracts/process_log.py`` 的步骤
条目用 ``presentation.step_action`` / ``completed_text`` 等函数生成文案；实时帧若用
内核的声明文案，同一行（``entry_id`` 相同、不会重复）在刷新前后会换措辞。
这里在 app 层注入同一套函数，保证**同一步骤的实时帧与刷新快照逐字一致**。
"""

from __future__ import annotations

from typing import Any

from app.agents.orchestration.models import Job


def _result_summary(result: dict | None, fallback: str = "") -> str:
    value = result or {}
    display = value.get("display")
    if isinstance(display, dict) and str(display.get("completed") or "").strip():
        return str(display["completed"])[:200]
    content = str(
        value.get("content") or value.get("output") or value.get("answer") or ""
    ).strip()
    if content:
        first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
        if first_line and len(first_line) <= 120:
            return first_line
        return (content[:96] + "…") if len(content) > 96 else content
    return str(fallback or "")[:200]


def _presentation_node(job: Job | None, step_id: str, step: dict | None) -> Any:
    """还原 ``presentation`` 文案所需的节点形状（与刷新投影同一解析规则）。

    ``app/contracts/process_log.py`` 用 ``job.nodes`` 里的节点（没有时退化成只用
    ``routing["steps"]`` 的轻量节点）调用 ``presentation.step_action`` 等函数；这里
    必须**逐字复用同一规则**，否则实时帧与刷新快照又会是两套措辞。内核只有声明
    文案（instruction/title），拿不到面向用户的表达，所以文案注入放在 app 层。
    """
    node = next((item for item in (job.nodes if job is not None else []) or [] if item.id == step_id), None)
    if node is not None:
        return node
    data = step if isinstance(step, dict) else {}
    from app.contracts.process_log import _StepNode

    return _StepNode(
        step_id=step_id,
        title=str(data.get("title") or ""),
        tool=str(data.get("tool") or ""),
    )


def _display_text(result: Any, key: str) -> str:
    """节点结果里的 ``display`` 文案（``attach_display_result`` 的落库副本）。"""
    if not isinstance(result, dict):
        return ""
    display = result.get("display")
    if not isinstance(display, dict):
        return ""
    return str(display.get(key) or "").strip()


def _text_or(step: Any, key: str) -> str:
    if not isinstance(step, dict):
        return ""
    return str(step.get(key) or "").strip()


def _live_presentation_fields(job: Job | None, event: dict) -> dict[str, str]:
    """按过程状态给出与刷新投影同源的 ``title``/``summary``（不改 ``entry_id``）。

    ``process_log_from_job()`` 的步骤条目 = ``title: step_action(node)`` +
    ``summary: intent/working/completed/failed_text(node, ...)``；实时帧若用内核
    的声明文案，同一行（``entry_id`` 相同、不会重复）在刷新前后会换措辞。这里在
    app 层注入同一套函数，使**同一步骤的实时帧与刷新快照逐字一致**。
    """
    from app.agents.orchestration.execution.presentation import (
        completed_text,
        failed_text,
        intent_text,
        step_action,
        working_text,
    )

    step_id = str(event.get("step_id") or "")
    if not step_id:
        return {}
    steps = (job.routing or {}).get("steps") if isinstance(job.routing, dict) else []
    step = next(
        (
            item
            for item in steps or []
            if isinstance(item, dict)
            and step_id in {str(item.get("id") or ""), str(item.get("step_id") or "")}
        ),
        None,
    )
    node = _presentation_node(job, step_id, step)
    result = step.get("result") if isinstance(step, dict) else None
    if not isinstance(result, dict):
        node_result = getattr(node, "result", None)
        result = node_result if isinstance(node_result, dict) else None
    status = str(event.get("status") or "running").casefold()
    if status == "completed":
        # 与刷新投影同源：刷新取节点 ``display.completed``（没有时才退到
        # ``routing.steps[].result_summary``）。实时帧发射时节点结果可能还没写回
        # 状态库，而 ``step_completed.result_summary`` **就是**引擎用同一个
        # ``_result_summary`` 算出的同一句话，因此这里优先用它，避免实时与刷新
        # 因为"快照早/晚一步"而换措辞。
        summary = _display_text(result, "completed")
        if not summary:
            summary = str(event.get("result_summary") or "").strip()
        if not summary:
            summary = str((step or {}).get("result_summary") or "").strip()
        if not summary:
            summary = completed_text(node, result)
    elif status == "failed":
        # 与刷新投影（``_step_entry`` → ``failed_text(node, step.error)``）同源。
        # 失败时状态库里的 ``step["error"]`` 与事件里的 ``result_summary`` 是**同一句**
        # 净化后的错误（内核 ``settle_failure`` 同时写两处）；节点/步骤已落地的错误
        # 优先，事件自带文本兜底，避免刷新前后措辞不同。
        error = (
            _display_text(result, "error")
            or _text_or(step, "error")
            or str(event.get("result_summary") or "").strip()
            or str(event.get("error") or "")
        )
        summary = failed_text(node, error or None)
    elif status == "pending":
        summary = intent_text(node)
    else:
        summary = working_text(node)
    return {"title": step_action(node), "summary": summary}


__all__ = [
    "_display_text",
    "_live_presentation_fields",
    "_presentation_node",
    "_result_summary",
    "_text_or",
]
