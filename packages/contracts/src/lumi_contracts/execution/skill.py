"""``SkillResult[T]``：组合 Skill（多步骤流程）的执行结果契约。

为什么与 ``ExecutionResult`` 分开（方案"四类核心对象"）：

* ``ToolRequest`` / ``ExecutionResult`` 描述**一次调用**；
* ``SkillResult`` 描述**一个 Skill 的整体结果**——它可能内部调用了多个工具，
  因此除了统一控制信息外，还必须能回答"每一步跑了什么、结果如何"；
* ``StreamEvent`` 描述流式过程。

两者不是重复：``SkillResult.to_execution_result()`` 让它进入统一的
``ExecutionResult`` + ``ProjectionRegistry`` 通道；反向
``from_execution_result()`` 让一次工具调用可以提升成 Skill 级结果。

业务数据仍然只在 ``payload`` 里，本类型只多一层"步骤账"。
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from lumi_contracts.common.errors import ErrorEnvelope
from lumi_contracts.common.status import ExecutionStatus
from lumi_contracts.common.version import SKILL_RESULT, ContractVersion, contract_version
from lumi_contracts.execution.artifacts import ArtifactRef, artifact_refs_from
from lumi_contracts.execution.result import ExecutionResult, ExecutionTiming

T = TypeVar("T")


class SkillStep(BaseModel):
    """Skill 内部的一次工具调用（可审计、可回放，不含正文）。"""

    name: str = ""
    index: int = 0
    status: ExecutionStatus = ExecutionStatus.SUCCESS
    call_id: str = ""
    tool: str = ""
    duration_ms: int = 0
    error_code: str = ""
    # 只放展示/排障用的短信息，禁止放大段正文。
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status.is_ok


class SkillResult(BaseModel, Generic[T]):
    """一个 Skill 的执行结果（统一控制信息 + 步骤账 + 业务 payload）。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    skill_name: str = ""
    namespace: str = "lumi"
    version: str = "1.0.0"

    status: ExecutionStatus = ExecutionStatus.SUCCESS
    payload: T | None = None
    steps: list[SkillStep] = Field(default_factory=list)

    error: ErrorEnvelope | None = None
    retryable: bool = False
    partial: bool = False
    timing: ExecutionTiming = Field(default_factory=ExecutionTiming)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    sensitivity: str = ""

    # 关联标识（服务端注入）。
    trace_id: str = ""
    request_id: str = ""
    call_id: str = ""
    job_id: str = ""
    node_id: str = ""

    # ── 派生 ──────────────────────────────────────────────

    @property
    def contract(self) -> ContractVersion:
        return SKILL_RESULT

    @property
    def ok(self) -> bool:
        return self.status.is_ok

    def step_counts(self) -> dict[str, int]:
        """按状态统计步骤数（审计/验收日志用，避免各自数数）。"""
        counts: dict[str, int] = {}
        for step in self.steps:
            key = str(step.status)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def to_execution_result(self, *, schema_name: str = "") -> ExecutionResult[T]:
        """进入统一结果/投影通道（``ExecutionResult``）。"""
        return ExecutionResult[T](
            status=self.status,
            payload=self.payload,
            schema_name=schema_name or f"lumi.{self.skill_name or 'skill'}.result",
            schema_version=1,
            tool_name=self.skill_name,
            namespace=self.namespace,
            trace_id=self.trace_id,
            request_id=self.request_id,
            call_id=self.call_id,
            job_id=self.job_id,
            node_id=self.node_id,
            error=self.error,
            retryable=self.retryable,
            partial=self.partial,
            timing=self.timing,
            artifact_refs=list(self.artifact_refs),
            sensitivity=self.sensitivity,
            # 步骤账作为审计元数据随结果走，不进入业务 payload。
            metadata={"skill_steps": [step.model_dump(mode="json", exclude_none=True) for step in self.steps]},
        )

    @classmethod
    def from_execution_result(
        cls,
        result: ExecutionResult[Any],
        *,
        skill_name: str = "",
        steps: list[SkillStep] | None = None,
    ) -> "SkillResult[Any]":
        """一次执行结果 → Skill 级结果（工具结果提升为 Skill 结果时使用）。"""
        return cls(
            skill_name=skill_name or result.tool_name,
            namespace=result.namespace or "lumi",
            status=result.status,
            payload=result.payload,
            steps=list(steps or []),
            error=result.error,
            retryable=result.retryable,
            partial=result.partial,
            timing=result.timing,
            artifact_refs=artifact_refs_from(result.artifact_refs),
            sensitivity=result.sensitivity,
            trace_id=result.trace_id,
            request_id=result.request_id,
            call_id=result.call_id,
            job_id=result.job_id,
            node_id=result.node_id,
        )


def skill_step_from_tool_output(
    value: Any,
    *,
    name: str = "",
    index: int = 0,
    tool: str = "",
) -> SkillStep:
    """任意工具结果（信封/对象/字典）→ 一个 ``SkillStep``（只取控制信息）。"""
    from lumi_contracts.adapters.legacy import adapt_tool_result

    result = adapt_tool_result(value, tool_name=tool)
    error_code = str(getattr(result.error, "code", "") or "")
    detail = str(getattr(result.error, "message", "") or "")
    return SkillStep(
        name=str(name or tool or result.tool_name or ""),
        index=int(index),
        status=result.status,
        call_id=str(result.call_id or ""),
        tool=str(tool or result.tool_name or ""),
        duration_ms=int(result.timing.duration_ms or 0),
        error_code=error_code,
        detail=detail[:300],
    )


def contract_version_for(payload: Any) -> ContractVersion:
    """按 payload 类型名给出默认结果契约版本（``lumi.<type>.result@1``）。"""
    name = type(payload).__name__.lower() if payload is not None else "skill"
    return contract_version(f"{name}.result", 1)


__all__ = [
    "SkillResult",
    "SkillStep",
    "contract_version_for",
    "skill_step_from_tool_output",
]
