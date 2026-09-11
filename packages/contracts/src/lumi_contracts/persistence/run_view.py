"""持久化契约：``JobRunView`` 等可恢复快照。

要求（方案第四、八节）：

* ``JobRunView`` 是**持久化快照**，不要直接拿 ``ExecutionResult`` 代替它；
* 刷新（GET /agents/jobs/{id}）后仍能恢复：状态、步骤、下一步动作、最终答复；
* 只放恢复所需的最小信息，正文按引用解析。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from lumi_contracts.events.lifecycle import RunState
from lumi_contracts.events.process import ProcessLogEntry

# 过程日志在快照里的条数上限（滚动窗口，避免状态库无界增长）。
PROCESS_LOG_MAX_ENTRIES = 200

# 最终答复在快照里的长度上限（可展示即可，正文另有引用）。
FINAL_ANSWER_MAX_CHARS = 20000


class StepView(BaseModel):
    """单步展示与恢复信息。"""

    id: str = ""
    title: str = ""
    status: str = ""
    runtime_status: str = ""
    tool: str = ""
    # 展示用短输出；完整结果按 result_ref 解析。
    output: str = ""
    error: str | None = None
    error_code: str | None = None
    depends_on: tuple[str, ...] = ()
    resource_claims: tuple[str, ...] = ()
    effect_status: str | None = None
    started_at: float | None = None
    completed_at: float | None = None
    duration_ms: int | None = None
    result_ref: dict[str, Any] | None = None


class JobRunView(BaseModel):
    """一次运行的持久化快照。"""

    job_id: str = ""
    conversation_id: str = ""
    status: RunState = RunState.PENDING
    # 计划优先：下一步动作（run_next 等），刷新后据此恢复。
    next_action: str = ""
    plan_text: str = ""
    plan_revision: int = 1
    steps: list[StepView] = Field(default_factory=list)
    # 覆盖度/路由等审计摘要（不含用户原文与正文）。
    routing: dict[str, Any] = Field(default_factory=dict)
    # 执行过程日志：安全摘要 + 去重键 + 状态；刷新后据此恢复过程气泡。
    # 只放过程（不含原始参数/原始响应/模型内部推理），正文走 result_ref/artifact。
    process_log: list[ProcessLogEntry] = Field(default_factory=list)
    final_answer: str = ""
    error: str | None = None
    error_code: str | None = None
    updated_at: float = 0.0
    version: int = 1

    @field_validator("final_answer")
    @classmethod
    def _bound_final_answer(cls, value: str) -> str:
        text = str(value or "")
        return text[:FINAL_ANSWER_MAX_CHARS]

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.INTERRUPTED
        }

    def with_process_log(self, entries: list[Any] | None) -> "JobRunView":
        """按去重键合并过程日志（SSE / 轮询 / 刷新叠加都不重复），并做滚动窗口。"""
        from lumi_contracts.events.process import merge_process_log

        merged = merge_process_log(self.process_log, entries, limit=PROCESS_LOG_MAX_ENTRIES)
        return self.model_copy(update={"process_log": merged})

    def to_snapshot(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


__all__ = ["FINAL_ANSWER_MAX_CHARS", "PROCESS_LOG_MAX_ENTRIES", "JobRunView", "StepView"]
