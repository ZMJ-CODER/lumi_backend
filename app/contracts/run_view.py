"""运行视图契约桥接（第四阶段）：``lumi_orch.run_view`` → ``JobRunView`` 校验。

``/agents/jobs/{id}`` 返回的 ``run_view`` 是前端"刷新后恢复"的唯一数据源，
形状漂移（未知状态、步骤丢失、正文超限）只有到前端才会暴露。这里把它投影成
契约 ``JobRunView`` 并做**可恢复性校验**：

* ``status`` 必须是契约 ``RunState``（否则前端按钮状态机无从判断）；
* ``steps`` 必须是列表，且每步有 id；
* ``final_answer`` 超出契约上限时会被截断（与契约模型一致）；
* 校验结果只写日志，**不改动接口输出**（前端形状保持原样）。
"""

from __future__ import annotations

from typing import Any

from lumi_contracts import JobRunView, RunState, StepView


def _state(value: Any) -> RunState | None:
    text = str(value or "")
    for item in RunState:
        if item.value == text:
            return item
    return None


def to_job_run_view(view: Any, *, conversation_id: str = "", routing: dict | None = None) -> JobRunView:
    """内核 ``run_view`` 字典 → 契约 ``JobRunView``。

    未知状态折叠为 ``PENDING``（契约模型不接受任意字符串），但**原始值会被
    :func:`run_view_problems` 报出来**，不会被静默当成正常状态。
    """
    source = view if isinstance(view, dict) else {}
    steps: list[StepView] = []
    raw_steps = source.get("steps")
    for raw in raw_steps if isinstance(raw_steps, list) else []:
        if not isinstance(raw, dict):
            continue
        steps.append(
            StepView(
                id=str(raw.get("id") or ""),
                title=str(raw.get("title") or ""),
                status=str(raw.get("status") or ""),
                runtime_status=str(raw.get("runtime_status") or ""),
                tool=str(raw.get("tool") or ""),
                output=str(raw.get("output") or ""),
                error=str(raw["error"]) if raw.get("error") else None,
                error_code=str(raw["error_code"]) if raw.get("error_code") else None,
                depends_on=tuple(str(item) for item in (raw.get("depends_on") or ())),
                resource_claims=tuple(str(item) for item in (raw.get("resource_claims") or ())),
                effect_status=str(raw["effect_status"]) if raw.get("effect_status") else None,
                started_at=raw.get("started_at"),
                completed_at=raw.get("completed_at"),
                duration_ms=raw.get("duration_ms"),
                result_ref=raw.get("result_ref") if isinstance(raw.get("result_ref"), dict) else None,
            )
        )
    return JobRunView(
        job_id=str(source.get("job_id") or ""),
        conversation_id=str(conversation_id or ""),
        status=_state(source.get("status")) or RunState.PENDING,
        next_action=str(source.get("next_action") or ""),
        plan_text=str(source.get("plan_text") or ""),
        plan_revision=int(source.get("plan_revision") or 1),
        steps=steps,
        routing=dict(routing or {}),
        final_answer=str(source.get("final_answer") or ""),
        error=str(source["error"]) if source.get("error") else None,
        error_code=str(source["error_code"]) if source.get("error_code") else None,
        updated_at=float(source.get("updated_at") or 0.0),
    )


def run_view_problems(view: Any, *, expected_job_id: str = "") -> list[str]:
    """可恢复性校验：返回问题列表（空列表表示通过）。"""
    if not isinstance(view, dict):
        return ["run_view 不是对象"]
    problems: list[str] = []
    job_id = str(view.get("job_id") or "")
    if not job_id:
        problems.append("缺少 job_id")
    elif expected_job_id and job_id != str(expected_job_id):
        problems.append("job_id 与请求的任务不一致")
    if _state(view.get("status")) is None:
        problems.append(f"未知运行状态：{view.get('status')!r}")
    if not str(view.get("next_action") or ""):
        problems.append("缺少 next_action（前端无法恢复按钮状态）")
    steps = view.get("steps")
    if not isinstance(steps, list):
        problems.append("steps 不是列表")
    else:
        for index, raw in enumerate(steps):
            if not isinstance(raw, dict):
                problems.append(f"第 {index} 个步骤不是对象")
            elif not str(raw.get("id") or ""):
                problems.append(f"第 {index} 个步骤缺少 id")
    return problems


__all__ = ["run_view_problems", "to_job_run_view"]
