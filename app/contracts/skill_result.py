"""``SkillResult`` 桥接（迁移期）：遗留 ``ToolOutput`` ↔ 契约 ``SkillResult[T]``。

为什么需要：Skill 级结果需要"步骤账"（跑了哪些工具、每步成功与否），而旧
``ToolOutput`` 只有一个扁平信封。这里把两者接起来：

* ``to_skill_result()``：任意遗留结果 → 契约 ``SkillResult``（可进投影通道）；
* ``skill_result_to_tool_output()``：契约 ``SkillResult`` → 旧 ``ToolOutput``
  （步骤账写进 ``meta.quality_hints["skill_steps"]``，其余字段保持不变）。

转换**无损**：给了 ``base`` 时以原信封为底，只补步骤账与状态，不重建字段。
"""

from __future__ import annotations

from typing import Any

from lumi_contracts import SkillResult, SkillStep

from app.agents.skills.output_contract import OutputMeta, ToolOutput


def to_skill_result(
    value: Any,
    *,
    skill_name: str = "",
    steps: list[SkillStep] | None = None,
) -> SkillResult[Any]:
    """遗留结果（``ToolOutput`` / 执行信封 / 契约结果）→ ``SkillResult``。"""
    from app.contracts import to_execution_result

    result = to_execution_result(value, tool_name=skill_name)
    return SkillResult.from_execution_result(result, skill_name=skill_name, steps=list(steps or []))


def skill_result_to_tool_output(result: SkillResult[Any], *, base: Any = None) -> ToolOutput:
    """``SkillResult`` → 旧 ``ToolOutput``（步骤账进 ``quality_hints``）。

    ``base`` 是产生该 SkillResult 的原始信封：给了就"以它为底"，只覆盖状态与
    步骤账，保证回程不丢 ``meta`` 扩展字段（workspace_id/transport 等）。
    """
    step_rows = [step.model_dump(mode="json", exclude_none=True) for step in result.steps]
    if base is not None:
        from app.services.tool_output_pipeline import normalize_execution_envelope

        output = normalize_execution_envelope(base)
        hints = dict(output.meta.quality_hints)
        if result.skill_name:
            hints["skill"] = result.skill_name
        if step_rows:
            hints["skill_steps"] = step_rows
        meta = output.meta.model_copy(update={"quality_hints": hints})
        return output.model_copy(update={"status": str(result.status), "meta": meta})

    error_code = str(getattr(result.error, "code", "") or "") or None
    error_message = str(getattr(result.error, "message", "") or "") or None
    payload = result.payload
    content_type = "structured" if isinstance(payload, (dict, list)) else "text"
    hints: dict[str, Any] = {}
    if result.skill_name:
        hints["skill"] = result.skill_name
    if step_rows:
        hints["skill_steps"] = step_rows
    return ToolOutput(
        status=str(result.status),
        call_id=str(result.call_id or "") or None,
        data=payload,
        content_type=content_type,
        meta=OutputMeta(
            total_size=len(payload) if isinstance(payload, str) else 0,
            summary=str(getattr(result.error, "message", "") or ""),
            quality_hints=hints,
            artifact_refs=[
                {
                    "ref_id": ref.ref_id,
                    "name": ref.name,
                    "media_type": ref.media_type,
                    "size": ref.size,
                }
                for ref in result.artifact_refs
            ],
        ),
        error=error_message,
        error_code=error_code,
        retryable=bool(result.retryable),
    )


__all__ = ["skill_result_to_tool_output", "to_skill_result"]
