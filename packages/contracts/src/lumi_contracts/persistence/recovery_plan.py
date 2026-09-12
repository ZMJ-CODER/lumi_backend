"""恢复计划契约（方案 §5 第 3–7 步）：把"能不能恢复、恢复哪些步骤"收敛成一个纯函数。

与 :mod:`lumi_contracts.persistence.effect_recovery` 的分工：

* ``effect_recovery`` 只回答"按副作用状态这一步该跳过/暂停/重排"；
* 本模块在此之上补齐恢复流程的**判定链**：

  1. 加载最近检查点（调用方的活）；
  2. **对比 Job revision 与 checkpoint_version**，旧检查点不得覆盖新状态；
  3. 找到未完成步骤；
  4. 对副作用步骤查 Effect Journal：``confirmed`` → 跳过；``pending``（可判定）→
     核对实际状态后再定（``RECONCILE_PENDING``）；``uncertain`` / 不可判定 → 暂停等人工；
  5. 明确未执行的 → 重新调度（带原 ``idempotency_key``）；
  6. 重新加载依赖 ``result_ref``（按引用 + 预算，属于调用方的 IO）。

本模块**只做决策**：不写库、不改 Job、不读 Redis。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from lumi_contracts.persistence.checkpoint import (
    StepCheckpoint,
    StepCheckpointState,
    checkpoint_is_stale,
    unresolved_side_effects,
)
from lumi_contracts.persistence.effect_recovery import (
    JOURNAL_CONFIRMED,
    JOURNAL_UNCERTAIN,
    RecoveryAction,
    canonical_status,
)


class RecoveryDecision(StrEnum):
    """整单恢复结论。"""

    #: 可以继续自动恢复（没有需要人工的步骤）。
    RESUME = "RESUME"
    #: 有步骤需要人工：暂停，等策略或人工决策。
    NEEDS_HUMAN = "NEEDS_HUMAN"
    #: 没有未完成步骤：任务其实已经结束（幂等重放/重复点击）。
    ALREADY_SETTLED = "ALREADY_SETTLED"


#: 恢复拒绝原因码（稳定错误码，禁止自由文本）。
REASON_RESUME = "RECOVERY_ALLOWED"
REASON_NEEDS_HUMAN = "EFFECT_UNCERTAIN_OR_IN_FLIGHT"
REASON_ALREADY_SETTLED = "NO_UNFINISHED_STEPS"


@dataclass(frozen=True, slots=True)
class StepPlan:
    """单个步骤的恢复决策。"""

    step_id: str
    action: str
    state: str = ""
    effect_status: str = ""
    reason_code: str = ""
    #: 重排时必须带上的原幂等键（方案 §5 第 6 步）。
    idempotency_key: str = ""
    #: 该步骤的结果引用（跳过时保留，重排时作废）。
    result_ref: dict[str, object] | None = None

    @property
    def auto_resume(self) -> bool:
        return self.action == RecoveryAction.RESUME.value

    @property
    def requires_human(self) -> bool:
        return self.action == RecoveryAction.PAUSE_FOR_HUMAN.value

    def as_dict(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "action": self.action,
            "state": self.state,
            "effect_status": self.effect_status,
            "reason_code": self.reason_code,
            "has_result_ref": self.result_ref is not None,
        }


@dataclass(frozen=True, slots=True)
class ResumePlan:
    """整单恢复计划（唯一红灯：``decision != RESUME`` 时不得自动继续）。"""

    job_id: str = ""
    decision: str = RecoveryDecision.RESUME.value
    steps: tuple[StepPlan, ...] = field(default_factory=tuple)
    #: Job revision 与检查点版本是否一致（``False`` 表示检查点陈旧，已回退到 Job 状态）。
    checkpoint_current: bool = True
    max_checkpoint_version: int = 0
    job_plan_revision: int = 1
    reason_code: str = ""
    #: 副作用状态无法判定 / 不可安全重排的步骤（暂停人工）。
    paused_step_ids: tuple[str, ...] = ()
    #: 需要先核对实际副作用状态的步骤。
    reconcile_step_ids: tuple[str, ...] = ()
    #: 已确认副作用、必须跳过的步骤。
    skip_step_ids: tuple[str, ...] = ()
    #: 可以重新调度（带原幂等键）的步骤。
    reschedule_step_ids: tuple[str, ...] = ()
    #: 陈旧检查点被忽略的步骤（以 Job 状态为准）。
    stale_step_ids: tuple[str, ...] = ()

    @property
    def resume_allowed(self) -> bool:
        return self.decision == RecoveryDecision.RESUME.value

    @property
    def requires_human(self) -> bool:
        return self.decision == RecoveryDecision.NEEDS_HUMAN.value

    def as_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "decision": self.decision,
            "resume_allowed": self.resume_allowed,
            "requires_human": self.requires_human,
            "checkpoint_current": self.checkpoint_current,
            "max_checkpoint_version": self.max_checkpoint_version,
            "job_plan_revision": self.job_plan_revision,
            "reason_code": self.reason_code,
            "skip_step_ids": list(self.skip_step_ids),
            "reschedule_step_ids": list(self.reschedule_step_ids),
            "reconcile_step_ids": list(self.reconcile_step_ids),
            "paused_step_ids": list(self.paused_step_ids),
            "stale_step_ids": list(self.stale_step_ids),
            "steps": [step.as_dict() for step in self.steps],
        }


def _is_settled(state: StepCheckpointState) -> bool:
    """该状态是否"不需要恢复关注"（``uncertain`` 不算：它等人工）。"""
    return state in {
        StepCheckpointState.COMPLETED,
        StepCheckpointState.FAILED,
        StepCheckpointState.CANCELLED,
    }


def _effect_status_of(
    checkpoint: StepCheckpoint,
    journal: Mapping[str, str],
    idempotency_keys: Mapping[str, str],
) -> tuple[str, str]:
    """返回 ``(副作用状态, 幂等键)``：检查点与 Journal 取更保守的一方。

    保守规则：只要有一处说"已确认"就是 ``confirmed``（跳过优先于重跑）；否则任一处
    说"在途/不确定"就沿用；两处都没有才是"未执行"。
    """
    key = str(idempotency_keys.get(checkpoint.step_id, "") or "").strip()
    journal_status = canonical_status(journal.get(key, "")) if key else ""
    checkpoint_status = canonical_status(checkpoint.effect_status or "")
    # 检查点本身就是 ``uncertain``：这是"副作用可能已发生但无法确认"的终态，
    # 必须按不确定处理（绝不当成"未执行"重跑）。
    if checkpoint.state is StepCheckpointState.UNCERTAIN:
        checkpoint_status = JOURNAL_UNCERTAIN
    for status in (JOURNAL_CONFIRMED,):
        if journal_status == status or checkpoint_status == status:
            return status, key
    if JOURNAL_UNCERTAIN in {journal_status, checkpoint_status}:
        return JOURNAL_UNCERTAIN, key
    return (journal_status or checkpoint_status), key


def plan_resume(
    *,
    job_id: str = "",
    job_plan_revision: int = 1,
    checkpoints: Iterable[StepCheckpoint] = (),
    journal: Mapping[str, str] | None = None,
    idempotency_keys: Mapping[str, str] | None = None,
    reconcilable: Iterable[str] = (),
    unfinished_step_ids: Iterable[str] | None = None,
    max_checkpoint_drift: int = 0,
) -> ResumePlan:
    """按方案 §5 给出整单恢复计划（纯函数）。

    :param checkpoints: 快照/检查点存储里读到的步骤检查点
    :param journal: ``idempotency_key → effect_status``
    :param idempotency_keys: ``step_id → idempotency_key``
    :param reconcilable: 宿主声明"在途副作用可核对"的步骤 id（可查文件/消息是否真的发生）
    :param unfinished_step_ids: 显式指定未完成步骤（缺省用检查点推导）
    :param max_checkpoint_drift: 允许的 ``plan_revision - checkpoint_version`` 漂移
    """
    rows = [item for item in checkpoints or () if isinstance(item, StepCheckpoint)]
    max_version = max((int(item.checkpoint_version or 0) for item in rows), default=0)
    # 第 3 步：Job revision vs checkpoint_version（旧检查点不得覆盖新状态）。
    stale = checkpoint_is_stale(
        checkpoint_version=max_version,
        job_plan_revision=int(job_plan_revision or 0),
        max_drift=max_checkpoint_drift,
    )
    effective = []
    stale_step_ids: list[str] = []
    for item in rows:
        if stale and int(item.checkpoint_version or 0) < int(job_plan_revision or 0):
            stale_step_ids.append(str(item.step_id))
            continue
        effective.append(item)

    # 第 4 步：找未完成步骤（``completed``/``failed``/``cancelled`` 之外都算，
    # ``uncertain`` 也在其中——它等人工决策，不能当已结束而漏掉可能的副作用）。
    if unfinished_step_ids is None:
        pending = [item for item in effective if not _is_settled(item.state)]
        pending.sort(key=lambda item: (int(item.checkpoint_version or 0), str(item.step_id)))
        ordered_ids = [str(item.step_id) for item in pending]
    else:
        ordered_ids = [str(item) for item in unfinished_step_ids if str(item)]
        pending = [item for item in effective if str(item.step_id) in set(ordered_ids)]

    by_step = {str(item.step_id): item for item in pending}
    allowed = {str(item) for item in (reconcilable or ()) if str(item)}
    steps: list[StepPlan] = []
    for step_id in ordered_ids:
        checkpoint = by_step.get(step_id)
        if checkpoint is None:
            # 没有检查点：按"未执行过副作用"处理（旧的 Job 状态路径）。
            steps.append(StepPlan(
                step_id=step_id,
                action=RecoveryAction.RESUME.value,
                reason_code="EFFECT_NOT_STARTED",
                idempotency_key=str((idempotency_keys or {}).get(step_id, "") or ""),
            ))
            continue
        status, key = _effect_status_of(checkpoint, journal or {}, idempotency_keys or {})
        if status == JOURNAL_CONFIRMED:
            steps.append(StepPlan(
                step_id=step_id,
                action=RecoveryAction.SKIP_CONFIRMED.value,
                state=checkpoint.state.value,
                effect_status=status,
                reason_code="EFFECT_ALREADY_COMMITTED",
                idempotency_key=key,
                result_ref=dict(checkpoint.result_ref) if checkpoint.result_ref else None,
            ))
            continue
        if status == JOURNAL_UNCERTAIN:
            steps.append(StepPlan(
                step_id=step_id,
                action=RecoveryAction.PAUSE_FOR_HUMAN.value,
                state=checkpoint.state.value,
                effect_status=status,
                reason_code="EFFECT_UNCERTAIN",
                idempotency_key=key,
            ))
            continue
        if status:
            # 在途（``pending`` / ``intent``）：能核对就先核对，不能核对就交人工。
            can_check = step_id in allowed
            steps.append(StepPlan(
                step_id=step_id,
                action=(
                    RecoveryAction.RECONCILE_PENDING.value
                    if can_check
                    else RecoveryAction.PAUSE_FOR_HUMAN.value
                ),
                state=checkpoint.state.value,
                effect_status=status,
                reason_code="EFFECT_IN_FLIGHT",
                idempotency_key=key,
            ))
            continue
        steps.append(StepPlan(
            step_id=step_id,
            action=RecoveryAction.RESUME.value,
            state=checkpoint.state.value,
            reason_code="EFFECT_NOT_STARTED",
            idempotency_key=key,
        ))

    paused = tuple(step.step_id for step in steps if step.requires_human)
    reconcile = tuple(step.step_id for step in steps if step.action == RecoveryAction.RECONCILE_PENDING.value)
    skipped = tuple(step.step_id for step in steps if step.action == RecoveryAction.SKIP_CONFIRMED.value)
    reschedule = tuple(step.step_id for step in steps if step.auto_resume)
    if not steps:
        decision = RecoveryDecision.ALREADY_SETTLED.value
        reason = REASON_ALREADY_SETTLED
    elif paused:
        decision = RecoveryDecision.NEEDS_HUMAN.value
        reason = REASON_NEEDS_HUMAN
    else:
        decision = RecoveryDecision.RESUME.value
        reason = REASON_RESUME
    return ResumePlan(
        job_id=str(job_id or ""),
        decision=decision,
        steps=tuple(steps),
        checkpoint_current=not stale,
        max_checkpoint_version=max_version,
        job_plan_revision=int(job_plan_revision or 1),
        reason_code=reason,
        paused_step_ids=paused,
        reconcile_step_ids=reconcile,
        skip_step_ids=skipped,
        reschedule_step_ids=reschedule,
        stale_step_ids=tuple(stale_step_ids),
    )


def reconciliation_targets(checkpoints: Iterable[StepCheckpoint]) -> tuple[str, ...]:
    """需要在恢复时核对实际副作用的步骤（``pending`` 或 ``uncertain`` 的检查点）。"""
    return tuple(
        sorted({str(item.step_id) for item in unresolved_side_effects(checkpoints)})
    )


def settle_reconciled_step(
    step_id: str,
    *,
    effect_happened: bool,
    result_ref: Mapping[str, object] | None = None,
) -> StepPlan:
    """在途副作用核对结果 → 单步决策。

    * 实际已发生 → ``SKIP_CONFIRMED``（幂等键已生效，**不重跑**）；
    * 实际未发生 → ``RESUME``（明确未执行，可重新调度，仍带原幂等键）。
    """
    if effect_happened:
        return StepPlan(
            step_id=str(step_id),
            action=RecoveryAction.SKIP_CONFIRMED.value,
            effect_status=JOURNAL_CONFIRMED,
            reason_code="EFFECT_RECONCILED_APPLIED",
            result_ref=dict(result_ref) if result_ref else None,
        )
    return StepPlan(
        step_id=str(step_id),
        action=RecoveryAction.RESUME.value,
        effect_status="",
        reason_code="EFFECT_RECONCILED_NOT_APPLIED",
    )


__all__ = [
    "REASON_ALREADY_SETTLED",
    "REASON_NEEDS_HUMAN",
    "REASON_RESUME",
    "RecoveryDecision",
    "ResumePlan",
    "StepPlan",
    "plan_resume",
    "reconciliation_targets",
    "settle_reconciled_step",
]
