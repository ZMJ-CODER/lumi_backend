"""恢复计划契约（方案 §5.4：副作用以 Effect Journal 为准）。

只做**决策面**，不做运行时改动：把"未完成步骤 + 副作用日志 → 每个步骤该怎么办"
收敛成一个纯函数，供既有恢复链路（``failed_job_recovery_service`` /
``skills.recovery`` / ``replan_service``）复用，避免三处各自判断。

判定表（顺序即优先级）：

============================  ====================  ==========================
未完成步骤的 effect_status    动作                  原因
============================  ====================  ==========================
``confirmed``                 ``SKIP_CONFIRMED``    副作用已确认执行，重跑会重复
``uncertain``                 ``PAUSE_FOR_HUMAN``   无法确认是否发生，不自动重跑
``pending``（可判定）          ``RECONCILE_PENDING``  查实际状态后再定（文件在不在）
``pending``（不可判定）        ``PAUSE_FOR_HUMAN``   信息不足，交人工
无记录（非副作用步骤）         ``RESUME``            未执行，可重新调度
============================  ====================  ==========================

``pending`` 是主口径，``intent`` 是等价别名（历史表沿用），两者判定一致。
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
    #: 副作用在途但**可判定**：查询实际状态（如文件是否存在）后收敛为
    #: ``SKIP_CONFIRMED`` 或 ``RESUME``——不直接重跑，也不直接叫人。
    RECONCILE_PENDING = "RECONCILE_PENDING"


#: Effect Journal 状态（与 ``effect_journal`` 表约束一致）。
#: ``pending`` 是主口径；``intent`` 是历史表/历史记录的等价别名（只收敛，不二次发明）。
JOURNAL_PENDING = "pending"
JOURNAL_INTENT = "intent"
JOURNAL_CONFIRMED = "confirmed"
JOURNAL_UNCERTAIN = "uncertain"

#: 在途状态集合（"请求已发出、未确认"）：都要先核对再决定，绝不自动重跑。
JOURNAL_IN_FLIGHT: frozenset[str] = frozenset({JOURNAL_PENDING, JOURNAL_INTENT})


def is_in_flight(status: object) -> bool:
    return str(status or "").strip().lower() in JOURNAL_IN_FLIGHT


def canonical_status(status: object) -> str:
    """归一副作用状态：``intent`` → ``pending``（对外只有一个口径）。"""
    text = str(status or "").strip().lower()
    if text == JOURNAL_INTENT:
        return JOURNAL_PENDING
    return text


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
    def reconcile_step_ids(self) -> tuple[str, ...]:
        """需要在途副作用核对（查实际状态）的步骤。"""
        return tuple(s.step_id for s in self.steps if s.action == RecoveryAction.RECONCILE_PENDING.value)

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
            "reconcile_step_ids": list(self.reconcile_step_ids),
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
    reconcilable: Iterable[str] | bool = False,
) -> RecoveryPlan:
    """按 §5.4 给出恢复计划。

    :param unfinished_step_ids: 快照里"未完成"的步骤 id（按原顺序）
    :param journal: ``idempotency_key → effect_status``（来自 Effect Journal）
    :param idempotency_keys: ``step_id → idempotency_key``（用于查 journal）
    :param reconcilable: 哪些步骤的在途副作用**可判定**（宿主声明"我能查实际状态"）。
        默认空集合：在途一律 ``PAUSE_FOR_HUMAN``（信息不足时绝不自动重跑）；
        传入步骤 id 后才对它们给出 ``RECONCILE_PENDING``。
    """
    raw_statuses = {str(k): canonical_status(v) for k, v in (journal or {}).items()}
    keys = {str(k): str(v) for k, v in (idempotency_keys or {}).items()}
    allowed: set[str] = {str(item) for item in (reconcilable or ()) if str(item)}
    plans: list[StepRecoveryPlan] = []
    for raw_step_id in unfinished_step_ids or ():
        step_id = str(raw_step_id or "")
        if not step_id:
            continue
        key = keys.get(step_id, "")
        status = raw_statuses.get(key, "") if key else ""
        if status == JOURNAL_CONFIRMED:
            plans.append(StepRecoveryPlan(
                step_id=step_id,
                action=RecoveryAction.SKIP_CONFIRMED.value,
                effect_status=status,
                reason_code="EFFECT_ALREADY_COMMITTED",
                message="该步骤的副作用已确认执行，跳过以免重复。",
            ))
        elif status == JOURNAL_UNCERTAIN:
            plans.append(StepRecoveryPlan(
                step_id=step_id,
                action=RecoveryAction.PAUSE_FOR_HUMAN.value,
                effect_status=status,
                reason_code="EFFECT_UNCERTAIN",
                requires_human=True,
                message="该步骤的副作用状态不确定，已暂停自动恢复，请人工确认后再继续。",
            ))
        elif status == JOURNAL_PENDING:
            # 在途：**默认**不自动重跑也不自动核对（``reconcilable`` 未声明时按保守处理）。
            # 宿主显式声明"这个步骤的在途副作用可判定"（例如写文件可查目标路径是否存在）
            # 时，才降级为 ``RECONCILE_PENDING``——先核对实际状态，再由调用方收敛为
            # 跳过或重排。两条路都不会盲目重跑。
            can_check = step_id in allowed
            plans.append(StepRecoveryPlan(
                step_id=step_id,
                action=(
                    RecoveryAction.RECONCILE_PENDING.value
                    if can_check
                    else RecoveryAction.PAUSE_FOR_HUMAN.value
                ),
                effect_status=status,
                reason_code="EFFECT_IN_FLIGHT",
                requires_human=not can_check,
                message=(
                    "该步骤的副作用已发出但未确认，需先核对实际状态后再决定是否重跑。"
                    if can_check
                    else "该步骤的副作用已发出但未确认，已暂停自动恢复，请人工确认后再继续。"
                ),
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
    "JOURNAL_IN_FLIGHT",
    "JOURNAL_INTENT",
    "JOURNAL_PENDING",
    "JOURNAL_UNCERTAIN",
    "RecoveryAction",
    "RecoveryPlan",
    "StepRecoveryPlan",
    "canonical_status",
    "is_in_flight",
    "plan_recovery",
]
