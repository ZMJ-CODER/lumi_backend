"""任务与步骤生命周期契约（状态机 + 流转合法性）。

把"什么状态可以走到什么状态"显式化，避免各模块各自 if/else 判断，导致
"已完成的任务又被 resume""失败节点被当成成功"这类问题。
"""

from __future__ import annotations

from enum import StrEnum


class RunState(StrEnum):
    PENDING = "pending"
    PLANNING = "planning"
    WAITING_RUN = "waiting_run"
    RUNNING = "running"
    RUNNING_STEP = "running_step"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_NEXT = "waiting_next"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


TERMINAL_STATES: frozenset[RunState] = frozenset({
    RunState.COMPLETED,
    RunState.FAILED,
    RunState.CANCELLED,
    RunState.INTERRUPTED,
})

# 允许的流转（显式表；新增状态必须补这里，否则流转被拒）。
_ALLOWED: dict[RunState, frozenset[RunState]] = {
    RunState.PENDING: frozenset({RunState.PLANNING, RunState.WAITING_RUN, RunState.RUNNING, RunState.CANCELLED}),
    RunState.PLANNING: frozenset({RunState.WAITING_RUN, RunState.RUNNING, RunState.FAILED, RunState.CANCELLED}),
    RunState.WAITING_RUN: frozenset({RunState.RUNNING, RunState.RUNNING_STEP, RunState.CANCELLED}),
    RunState.RUNNING: frozenset({RunState.RUNNING_STEP, RunState.WAITING_APPROVAL, RunState.WAITING_NEXT,
                                 RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.INTERRUPTED}),
    RunState.RUNNING_STEP: frozenset({RunState.RUNNING, RunState.WAITING_APPROVAL, RunState.WAITING_NEXT,
                                      RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.INTERRUPTED}),
    RunState.WAITING_APPROVAL: frozenset({RunState.RUNNING, RunState.RUNNING_STEP, RunState.FAILED,
                                          RunState.CANCELLED}),
    RunState.WAITING_NEXT: frozenset({RunState.RUNNING_STEP, RunState.RUNNING, RunState.COMPLETED,
                                      RunState.FAILED, RunState.CANCELLED}),
}


def can_transition(current: RunState | str, target: RunState | str) -> bool:
    """判断状态流转是否合法；终态不可再流转（除非显式取消）。"""
    source = RunState(str(current))
    destination = RunState(str(target))
    if source == destination:
        return True
    if source in TERMINAL_STATES:
        return False
    return destination in _ALLOWED.get(source, frozenset())


def assert_transition(current: RunState | str, target: RunState | str) -> None:
    if not can_transition(current, target):
        from lumi_contracts.common.errors import ContractError, ContractErrorCode

        raise ContractError(
            ContractErrorCode.INVALID_CONTRACT,
            f"非法状态流转：{current} → {target}",
            details={"current": str(current), "target": str(target)},
        )


__all__ = ["RunState", "TERMINAL_STATES", "assert_transition", "can_transition"]
