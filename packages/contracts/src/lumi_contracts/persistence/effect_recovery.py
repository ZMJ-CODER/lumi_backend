"""恢复计划契约（方案 §5.4：副作用以 Effect Journal 为准）。

只做**决策面**，不做运行时改动：把"未完成步骤 + 副作用日志 → 每个步骤该怎么办"
收敛成一个纯函数，供既有恢复链路（``failed_job_recovery_service`` /
``skills.recovery`` / ``replan_service``）复用，避免三处各自判断。

判定表（顺序即优先级）：

============================  ====================  ==========================
未完成步骤的 effect_status    动作                  原因
============================  ====================  ==========================
``confirmed``                 ``SKIP_CONFIRMED``    副作用已确认执行，重跑会重复
``uncertain``                 ``PAUSE_FOR_HUMAN``   可能已执行，不自动重跑
``intent``                    ``PAUSE_FOR_HUMAN``   已开始未确认（等于在途）
无记录（非副作用步骤）         ``RESUME``            未执行，可重新调度
============================  ====================  ==========================

``resume_allowed`` 只有在**没有任何步骤需要人工**时才为真——这条是给恢复链路用的
唯一红灯，避免"部分暂停但整体继续跑"导致脏副作用。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class RecoveryAction(StrEnum):
    """单个未完成步骤的恢复动作。"""

    SKIP_CONFIRMED = "SKIP_CONFIRMED"
    RESUME = "RESUME"
    PAUSE_FOR_HUMAN = "PAUSE_FOR_HUMAN"


#: Effect Journal 状态（与 ``effect_journal`` 表约束一致）。
JOURNAL_INTENT = "intent"
JOURNAL_CONFIRMED = "confirmed"
JOURNAL_UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class StepRecoveryPlan:
    """单个步骤的恢复决策。"""

    step_id: str
    action: str
    effect_status: str = ""
    reason_code: str = ""
    requires_human: bool = False
    message: str = ""


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    """整体恢复计划（只读；调用方据此决定跳过/重排/暂停）。"""

    steps: tuple[StepRecoveryPlan, ...] = field(default_factory=tuple)

    @property
    def skip_step_ids(self) -> tuple[str, ...]:
        return tuple(s.step_id for s in self.steps if s.action == RecoveryAction.SKIP_CONFIRMED.value)

    @property
    def reschedule_step_ids(self) -> tuple[str, ...]:
        return tuple(s.step_id for s in self.steps if s.action == RecoveryAction.RESUME.value)

    @property
    def paused_step_ids(self) -> tuple[str, ...]:
        return tuple(s.step_id for s in self.steps if s.action == RecoveryAction.PAUSE_FOR_HUMAN.value)

    @property
    def requires_human(self) -> bool:
        return bool(self.paused_step_ids)

    @property
    def resume_allowed(self) -> bool:
        """有任何一个步骤需要人工 → 不允许继续自动恢复。"""
        return not self.requires_human

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return tuple(s.reason_code for s in self.steps if s.reason_code)

    def as_dict(self) -> dict[str, object]:
        return {
            "resume_allowed": self.resume_allowed,
            "requires_human": self.requires_human,
            "skip_step_ids": list(self.skip_step_ids),
            "reschedule_step_ids": list(self.reschedule_step_ids),
            "paused_step_ids": list(self.paused_step_ids),
            "steps": [
                {
                    "step_id": s.step_id,
                    "action": s.action,
                    "effect_status": s.effect_status,
                    "reason_code": s.reason_code,
                }
                for s in self.steps
            ],
        }


def plan_recovery(
    unfinished_step_ids: Iterable[str],
    journal: Mapping[str, str] | None = None,
    *,
    idempotency_keys: Mapping[str, str] | None = None,
) -> RecoveryPlan:
    """按 §5.4 给出恢复计划。

    :param unfinished_step_ids: 快照里"未完成"的步骤 id（按原顺序）
    :param journal: ``idempotency_key → effect_status``（来自 Effect Journal）
    :param idempotency_keys: ``step_id → idempotency_key``（用于查 journal）
    """
    statuses = {str(k): str(v) for k, v in (journal or {}).items()}
    keys = {str(k): str(v) for k, v in (idempotency_keys or {}).items()}
    plans: list[StepRecoveryPlan] = []
    for raw_step_id in unfinished_step_ids or ():
        step_id = str(raw_step_id or "")
        if not step_id:
            continue
        key = keys.get(step_id, "")
        status = statuses.get(key, "") if key else ""
        if status == JOURNAL_CONFIRMED:
            plans.append(StepRecoveryPlan(
                step_id=step_id,
                action=RecoveryAction.SKIP_CONFIRMED.value,
                effect_status=status,
                reason_code="EFFECT_ALREADY_COMMITTED",
                message="该步骤的副作用已确认执行，跳过以免重复。",
            ))
        elif status in {JOURNAL_UNCERTAIN, JOURNAL_INTENT}:
            plans.append(StepRecoveryPlan(
                step_id=step_id,
                action=RecoveryAction.PAUSE_FOR_HUMAN.value,
                effect_status=status,
                reason_code="EFFECT_UNCERTAIN" if status == JOURNAL_UNCERTAIN else "EFFECT_IN_FLIGHT",
                requires_human=True,
                message="该步骤的副作用状态不确定，已暂停自动恢复，请人工确认后再继续。",
            ))
        else:
            plans.append(StepRecoveryPlan(
                step_id=step_id,
                action=RecoveryAction.RESUME.value,
                reason_code="EFFECT_NOT_STARTED",
                message="该步骤未执行过副作用，可重新调度。",
            ))
    return RecoveryPlan(steps=tuple(plans))


__all__ = [
    "JOURNAL_CONFIRMED",
    "JOURNAL_INTENT",
    "JOURNAL_UNCERTAIN",
    "RecoveryAction",
    "RecoveryPlan",
    "StepRecoveryPlan",
    "plan_recovery",
]
