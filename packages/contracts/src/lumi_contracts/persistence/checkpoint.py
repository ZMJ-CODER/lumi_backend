"""步骤检查点契约：状态机 + 落盘记录（方案第二节）。

设计原则（方案 §2.1–§2.3）：

* **执行器外层统一协调**：Tool / Skill / LLM 节点自己不保存状态，由
  ``StepCheckpointCoordinator``（宿主实现）在**每步前后**写检查点；
* **状态是枚举**：``planned → started → running → waiting_approval →
  completed | failed | cancelled | uncertain``，非法跃迁必须被拒绝（不是"尽力而为"）；
* **完成事件在检查点落盘之后**：否则会出现"前端看到步骤完成、刷新后找不到结果"。
  契约层把它表达为 :func:`assert_emit_after_persist` 这条唯一的时序检查；
* **``checkpoint_version`` 单调递增**：恢复时用它对比 Job revision，防止旧检查点
  覆盖新状态（方案 §5 第 3 步）。

本模块只做**决策与校验**，不写存储、不 import ``app``。
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

#: 检查点契约版本（破坏性改字段才升）。
CHECKPOINT_CONTRACT_VERSION = 1

#: 状态机版本：与 ``checkpoint_contract_version`` 分开，表示"迁移规则"版本。
CHECKPOINT_VERSION = 1


class StepCheckpointState(StrEnum):
    """步骤状态（方案 §2.2 唯一的枚举口径）。"""

    PLANNED = "planned"
    STARTED = "started"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: 副作用可能已发生但无法确认：**绝不自动重跑**，等策略或人工决策。
    UNCERTAIN = "uncertain"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL

    @property
    def is_ok(self) -> bool:
        return self is StepCheckpointState.COMPLETED


#: 终态：不再推进（``uncertain`` 也是终态——它等人工，不自动前进）。
_TERMINAL: frozenset[StepCheckpointState] = frozenset(
    {
        StepCheckpointState.COMPLETED,
        StepCheckpointState.FAILED,
        StepCheckpointState.CANCELLED,
        StepCheckpointState.UNCERTAIN,
    }
)

#: 合法跃迁表（``from → 允许到达的 to``）。终态在表里为空集合。
ALLOWED_TRANSITIONS: dict[StepCheckpointState, frozenset[StepCheckpointState]] = {
    StepCheckpointState.PLANNED: frozenset(
        {
            StepCheckpointState.STARTED,
            # 既有执行内核在进入节点前就把步骤标成 running（``mark_running``），
            # 协调器第一次看到它时状态已经是 running。这条捷径必须合法，否则
            # 既有状态机接入检查点后会写不出第一步。
            StepCheckpointState.RUNNING,
            StepCheckpointState.CANCELLED,
            StepCheckpointState.WAITING_APPROVAL,
        }
    ),
    StepCheckpointState.STARTED: frozenset(
        {
            StepCheckpointState.RUNNING,
            StepCheckpointState.WAITING_APPROVAL,
            StepCheckpointState.COMPLETED,
            StepCheckpointState.FAILED,
            StepCheckpointState.CANCELLED,
            StepCheckpointState.UNCERTAIN,
        }
    ),
    StepCheckpointState.RUNNING: frozenset(
        {
            StepCheckpointState.WAITING_APPROVAL,
            StepCheckpointState.COMPLETED,
            StepCheckpointState.FAILED,
            StepCheckpointState.CANCELLED,
            StepCheckpointState.UNCERTAIN,
        }
    ),
    # 审批后继续：回到 running（经 STARTED 亦可，保持两种实现都能收敛）。
    StepCheckpointState.WAITING_APPROVAL: frozenset(
        {
            StepCheckpointState.STARTED,
            StepCheckpointState.RUNNING,
            StepCheckpointState.COMPLETED,
            StepCheckpointState.FAILED,
            StepCheckpointState.CANCELLED,
            StepCheckpointState.UNCERTAIN,
        }
    ),
    StepCheckpointState.COMPLETED: frozenset(),
    StepCheckpointState.FAILED: frozenset(),
    StepCheckpointState.CANCELLED: frozenset(),
    StepCheckpointState.UNCERTAIN: frozenset(),
}

#: 终态集合（供调用方按名使用，避免各处再写一遍字面量）。
TERMINAL_STATES: frozenset[str] = frozenset(state.value for state in _TERMINAL)

#: 状态别名（旧快照/旧事件的写法 → 契约状态）：只收敛，不二次发明。
STATE_ALIASES: dict[str, str] = {
    "pending": StepCheckpointState.PLANNED.value,
    "queued": StepCheckpointState.PLANNED.value,
    "intent": StepCheckpointState.STARTED.value,
    "in_progress": StepCheckpointState.RUNNING.value,
    "executing": StepCheckpointState.RUNNING.value,
    "waiting_approval": StepCheckpointState.WAITING_APPROVAL.value,
    "approval": StepCheckpointState.WAITING_APPROVAL.value,
    "success": StepCheckpointState.COMPLETED.value,
    "succeeded": StepCheckpointState.COMPLETED.value,
    "done": StepCheckpointState.COMPLETED.value,
    "error": StepCheckpointState.FAILED.value,
    "canceled": StepCheckpointState.CANCELLED.value,
    "uncertain": StepCheckpointState.UNCERTAIN.value,
    "unknown": StepCheckpointState.UNCERTAIN.value,
    # 既有执行内核的"等待写资源不可用"：回滚到等待态，语义上等于没开始。
    "waiting_resources": StepCheckpointState.PLANNED.value,
    "waiting_run": StepCheckpointState.PLANNED.value,
    "planning": StepCheckpointState.PLANNED.value,
    "retrying": StepCheckpointState.PLANNED.value,
    "skipped": StepCheckpointState.CANCELLED.value,
}

#: 生命周期次序（用于"检查点只前进、不回退"的单调判定）。
#: ``PLANNED`` 最小；三个结束态（失败/取消/不确定）与完成态同级，互相不回退。
STATE_RANK: dict[StepCheckpointState, int] = {
    StepCheckpointState.PLANNED: 0,
    StepCheckpointState.STARTED: 1,
    StepCheckpointState.RUNNING: 2,
    StepCheckpointState.WAITING_APPROVAL: 3,
    StepCheckpointState.COMPLETED: 4,
    StepCheckpointState.FAILED: 4,
    StepCheckpointState.CANCELLED: 4,
    StepCheckpointState.UNCERTAIN: 4,
}


def is_regression(current: StepCheckpointState | str, target: StepCheckpointState | str) -> bool:
    """目标状态是否**落后于**当前状态（回退）。

    既有执行内核允许步骤状态回滚（例如写资源不可用时 ``revert_to_waiting`` 把步骤
    退回 ``pending``）。检查点是**结论**，一旦推进就不该被回退覆盖——否则恢复时会看到
    "已经跑过的步骤又变成没跑"，进而重跑并产生重复副作用。
    """
    source = normalize_state(current)
    destination = normalize_state(target)
    if source is None or destination is None:
        return False
    return STATE_RANK.get(destination, 0) < STATE_RANK.get(source, 0)


def normalize_state(value: object) -> StepCheckpointState | None:
    """任意来源的状态字符串 → 契约状态（无法识别返回 ``None``，不猜）。"""
    text = str(getattr(value, "value", value) or "").strip().lower()
    if not text:
        return None
    for state in StepCheckpointState:
        if state.value == text:
            return state
    alias = STATE_ALIASES.get(text)
    if alias:
        return StepCheckpointState(alias)
    return None


def can_transition(current: StepCheckpointState | str, target: StepCheckpointState | str) -> bool:
    """状态跃迁是否合法。"""
    source = normalize_state(current)
    destination = normalize_state(target)
    if source is None or destination is None:
        return False
    if source is destination:
        return True
    return destination in ALLOWED_TRANSITIONS.get(source, frozenset())


def assert_transition(current: StepCheckpointState | str, target: StepCheckpointState | str) -> None:
    """非法跃迁直接拒绝（同一状态重复写入是幂等的，不算跃迁）。"""
    if can_transition(current, target):
        return
    raise ValueError(f"非法步骤状态跃迁：{current} → {target}")


# ── 运行状态与检查点的映射（方案 §2.2 的运行时状态）──────────


class StepRuntimeStatus(StrEnum):
    """运行时状态（与检查点状态区分：检查点是"落盘的结论"）。"""

    IDLE = "idle"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SETTLED = "settled"


#: 检查点终态 → 运行时状态。
RUNTIME_STATUS_BY_STATE: dict[StepCheckpointState, StepRuntimeStatus] = {
    StepCheckpointState.PLANNED: StepRuntimeStatus.IDLE,
    StepCheckpointState.STARTED: StepRuntimeStatus.RUNNING,
    StepCheckpointState.RUNNING: StepRuntimeStatus.RUNNING,
    StepCheckpointState.WAITING_APPROVAL: StepRuntimeStatus.WAITING_APPROVAL,
    StepCheckpointState.COMPLETED: StepRuntimeStatus.SETTLED,
    StepCheckpointState.FAILED: StepRuntimeStatus.SETTLED,
    StepCheckpointState.CANCELLED: StepRuntimeStatus.SETTLED,
    StepCheckpointState.UNCERTAIN: StepRuntimeStatus.SETTLED,
}


def runtime_status_for(state: StepCheckpointState | str) -> str:
    normalized = normalize_state(state)
    if normalized is None:
        return StepRuntimeStatus.IDLE.value
    return RUNTIME_STATUS_BY_STATE[normalized].value


class StepCheckpoint(BaseModel):
    """一步的检查点记录（方案 §2.2 的全部字段）。

    这是**落盘记录**：只有摘要与引用，完整结果按 ``result_ref`` 解析。
    """

    checkpoint_contract_version: int = CHECKPOINT_CONTRACT_VERSION
    job_id: str = ""
    step_id: str = ""
    #: 同一步骤的第几次尝试（与 ``(job_id, step_id, attempt)`` 唯一键一致）。
    attempt: int = 1
    tool_name: str = ""
    step_type: str = ""
    #: 副作用类型（``file_create`` / ``send`` / ``unknown`` …）：决定恢复时"怎么核对"。
    effect_type: str = ""
    #: 副作用幂等键（目标路径 hash / 消息 ID）：重排时必须带上，避免重复副作用。
    idempotency_key: str = ""
    status: StepCheckpointState = StepCheckpointState.PLANNED
    started_at: float = 0.0
    finished_at: float = 0.0
    #: 输入摘要（参数 sha256，**不是**原始参数）。
    input_digest: str = ""
    #: 输出摘要（可展示短文本）。
    output_summary: str = ""
    #: 结果引用（只存最小引用）。
    result_ref: dict[str, Any] | None = None
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list)
    error_code: str = ""
    #: 副作用状态（与 Effect Journal 三态一致：``confirmed`` / ``pending`` / ``uncertain``）。
    effect_status: str = ""
    #: 检查点版本：单调递增，恢复时用于防止旧检查点覆盖新状态。
    checkpoint_version: int = 0
    updated_at: float = 0.0

    @property
    def state(self) -> StepCheckpointState:
        return normalize_state(self.status) or StepCheckpointState.PLANNED

    @property
    def key(self) -> tuple[str, str, int]:
        return (str(self.job_id), str(self.step_id), int(self.attempt or 1))

    @property
    def is_terminal(self) -> bool:
        return self.state.is_terminal

    def advanced_to(
        self,
        status: StepCheckpointState | str,
        *,
        now: float | None = None,
        **changes: Any,
    ) -> "StepCheckpoint":
        """推进到新状态（非法跃迁抛 ``ValueError``；同状态是幂等更新）。"""
        target = normalize_state(status)
        if target is None:
            raise ValueError(f"未知步骤状态：{status!r}")
        assert_transition(self.state, target)
        moment = float(now if now is not None else time.time())
        update: dict[str, Any] = {
            "status": target,
            "checkpoint_version": int(self.checkpoint_version or 0) + 1,
            "updated_at": moment,
        }
        if target is not StepCheckpointState.PLANNED and not self.started_at:
            update["started_at"] = moment
        if target.is_terminal:
            update["finished_at"] = moment
        for name, value in changes.items():
            if name in StepCheckpoint.model_fields:
                update[name] = value
        return self.model_copy(update=update)

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


# ── 事件发射时序（方案 §2.3 铁律）─────────────────────────

#: 完成类事件：**必须**在检查点（含 ``result_ref``）落盘成功之后才能发。
PERSIST_THEN_EMIT_EVENTS: frozenset[str] = frozenset(
    {
        "step_completed",
        "step_failed",
        "artifact_created",
        "waiting_next",
        "task_completed",
        "task_failed",
    }
)


class CheckpointOrderingError(RuntimeError):
    """完成事件早于检查点落盘（会造成"前端看到完成、刷新后没有结果"）。"""


def must_persist_before_emit(event_type: str) -> bool:
    return str(event_type or "").strip() in PERSIST_THEN_EMIT_EVENTS


def assert_emit_after_persist(
    event_type: str,
    *,
    persisted_version: int,
    current_version: int,
) -> None:
    """时序校验：发完成事件前，检查点必须已落盘且版本不落后。

    :param persisted_version: 检查点**已落盘**的 ``checkpoint_version``（未落盘为 0）
    :param current_version: 内存中该步骤当前的 ``checkpoint_version``
    """
    if not must_persist_before_emit(event_type):
        return
    if int(persisted_version) <= 0:
        raise CheckpointOrderingError(f"{event_type} 必须在检查点落盘之后发送（当前未落盘）")
    if int(persisted_version) < int(current_version):
        raise CheckpointOrderingError(
            f"{event_type} 对应的检查点版本落后（落盘 {persisted_version} < 当前 {current_version}）"
        )


# ── 恢复对比（方案 §5 第 3 步）──────────────────────────────


def checkpoint_is_stale(*, checkpoint_version: int, job_plan_revision: int, max_drift: int = 0) -> bool:
    """旧检查点判定：``job_plan_revision`` 领先检查点超过 ``max_drift`` 即为陈旧。

    计划被重规划（``plan_revision`` 前进）后，早于它的检查点不得再用于恢复——
    否则会把"已经被替换掉的步骤结论"当成当前状态。
    """
    try:
        version = int(checkpoint_version or 0)
        revision = int(job_plan_revision or 0)
    except (TypeError, ValueError):
        return True
    if version <= 0:
        return True
    if revision <= 0:
        return False
    return revision - version > max(0, int(max_drift))


def unfinished_steps(checkpoints: Iterable[StepCheckpoint]) -> tuple[StepCheckpoint, ...]:
    """未完成步骤（按 ``checkpoint_version`` 升序稳定输出）。

    ``uncertain`` **算未完成**：虽然它不再自动前进，但它等的是人工决策——如果把它当
    已完成，恢复流程就会漏掉"可能已经发生的副作用"，这正是方案要防的重复副作用。
    """
    rows = [item for item in checkpoints if item.state not in _SETTLED_FOR_RESUME]
    rows.sort(key=lambda item: (int(item.checkpoint_version or 0), str(item.step_id)))
    return tuple(rows)


#: 不再需要恢复关注的终态（``uncertain`` 不在其中，见 :func:`unfinished_steps`）。
_SETTLED_FOR_RESUME: frozenset[StepCheckpointState] = frozenset(
    {StepCheckpointState.COMPLETED, StepCheckpointState.FAILED, StepCheckpointState.CANCELLED}
)


def unresolved_side_effects(checkpoints: Iterable[StepCheckpoint]) -> tuple[StepCheckpoint, ...]:
    """副作用未定局的步骤：``uncertain``，或已定局但副作用并非 ``confirmed``。"""
    from lumi_contracts.persistence.effect_recovery import JOURNAL_CONFIRMED

    rows: list[StepCheckpoint] = []
    for item in checkpoints:
        status = str(item.effect_status or "").strip()
        if item.state is StepCheckpointState.UNCERTAIN or (status and status != JOURNAL_CONFIRMED):
            rows.append(item)
    return tuple(rows)

def checkpoint_summary(checkpoints: Iterable[StepCheckpoint]) -> dict[str, Any]:
    """检查点集合的紧凑摘要（进快照/日志；不含正文）。"""
    rows = list(checkpoints)
    by_state: dict[str, int] = {}
    for item in rows:
        key = item.state.value
        by_state[key] = by_state.get(key, 0) + 1
    return {
        "total": len(rows),
        "by_state": by_state,
        "unfinished": len(unfinished_steps(rows)),
        "unresolved_effects": len(unresolved_side_effects(rows)),
        "max_checkpoint_version": max((int(item.checkpoint_version or 0) for item in rows), default=0),
    }


def merge_checkpoint(
    existing: Mapping[tuple[str, str, int], StepCheckpoint],
    incoming: StepCheckpoint,
) -> StepCheckpoint:
    """按 ``(job_id, step_id, attempt)`` 合并检查点：旧版本不得覆盖新状态。"""
    previous = existing.get(incoming.key)
    if previous is None:
        return incoming
    if int(previous.checkpoint_version or 0) > int(incoming.checkpoint_version or 0):
        return previous
    return incoming


__all__ = [
    "ALLOWED_TRANSITIONS",
    "CHECKPOINT_CONTRACT_VERSION",
    "CHECKPOINT_VERSION",
    "PERSIST_THEN_EMIT_EVENTS",
    "RUNTIME_STATUS_BY_STATE",
    "STATE_ALIASES",
    "STATE_RANK",
    "TERMINAL_STATES",
    "CheckpointOrderingError",    "StepCheckpoint",
    "StepCheckpointState",
    "StepRuntimeStatus",
    "assert_emit_after_persist",
    "assert_transition",
    "can_transition",
    "checkpoint_is_stale",
    "checkpoint_summary",
    "is_regression",
    "merge_checkpoint",
    "must_persist_before_emit",
    "normalize_state",
    "runtime_status_for",
    "unfinished_steps",
    "unresolved_side_effects",
]
