"""任务恢复服务（方案 §5）：不是"找最后一个成功步骤继续"，而是**按事实核对**。

七步流程的落点：

1. 加载 Redis Job State（事实源：任务控制面）；
2. 加载步骤检查点（``multiagent:step_checkpoints:{job_id}``）；
3. **对比 Job revision 与 checkpoint_version**：旧检查点不得覆盖新状态
   （``plan_revision`` 领先检查点时，该检查点被判陈旧，回退到 Job 状态）；
4. 找到未完成步骤；
5. 对副作用步骤查 Effect Journal：``confirmed`` → 跳过；``pending``（可判定）→
   先核对实际状态（查文件是否存在 / 消息是否已发）；``uncertain`` / 不可判定 → 暂停等人工；
6. 明确未执行的 → 重新调度（带原 ``idempotency_key``）；
7. 重新加载依赖 ``result_ref``（按引用 + 预算）。

本模块**只读 + 只出计划**：不改 Job、不写库、不重跑。调用方（恢复入口 / 编排服务）
拿到 :class:`~lumi_contracts.persistence.recovery_plan.ResumePlan` 后自行决定是否继续。

在途副作用的"核对"通过 :class:`EffectReconciler` 协议注入：宿主提供"这个副作用到底发生
了没有"的判定（文件系统 / 消息服务 / 外部系统查询），本模块不猜。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from loguru import logger

from lumi_contracts.persistence.checkpoint import StepCheckpoint, unresolved_side_effects
from lumi_contracts.persistence.effect_recovery import (
    JOURNAL_CONFIRMED,
    JOURNAL_PENDING,
    JOURNAL_UNCERTAIN,
    RecoveryAction,
    canonical_status,
)
from lumi_contracts.persistence.recovery_plan import (
    ResumePlan,
    plan_resume,
)
from lumi_contracts.persistence.result_store import LoadBudget


class EffectReconciler(Protocol):
    """在途副作用核对端口：回答"这个副作用到底发生了没有"。"""

    async def effect_applied(self, record: Mapping[str, Any]) -> bool | None:
        """``True`` 已发生 / ``False`` 明确未发生 / ``None`` 无法判定（→ 交人工）。"""
        ...


@dataclass(frozen=True, slots=True)
class DependencyLoad:
    """一次依赖结果重载的结论（方案 §5 第 7 步）。"""

    step_id: str
    body: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False
    error_code: str = ""
    message: str = ""

    @property
    def ok(self) -> bool:
        return not self.error_code


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """恢复前的完整核对报告（可审计、可进接口，不含正文）。"""

    plan: ResumePlan
    journal_available: bool = True
    reconciled_step_ids: tuple[str, ...] = ()
    dependencies: tuple[DependencyLoad, ...] = ()
    generated_at: float = 0.0

    @property
    def resume_allowed(self) -> bool:
        return self.plan.resume_allowed and self.journal_available

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.plan.as_dict(),
            "journal_available": self.journal_available,
            "reconciled_step_ids": list(self.reconciled_step_ids),
            "dependencies": [
                {
                    "step_id": item.step_id,
                    "truncated": item.truncated,
                    "error_code": item.error_code,
                }
                for item in self.dependencies
            ],
            "generated_at": self.generated_at,
        }


async def _default_journal(job_id: str) -> tuple[dict[str, dict[str, Any]], bool]:
    """读任务的副作用记录（``key → record``，是否可读）。"""
    try:
        from app.repositories.effect_journal_repository import PostgresEffectJournalRepository

        repository = PostgresEffectJournalRepository()
        rows = await repository.list_for_job(str(job_id))
    except Exception as exc:  # noqa: BLE001 - 日志不可用必须 fail-closed（由 available 表达）
        logger.debug("[recovery] 副作用日志不可用 job={}: {}", str(job_id)[:12], str(exc)[:120])
        return {}, False
    out: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        key = str(row.get("effect_key") or "")
        if not key:
            intent = row.get("intent") if isinstance(row.get("intent"), dict) else {}
            key = str(intent.get("params_sha256") or "")
        if key:
            out[key] = dict(row)
    return out, True


def journal_statuses(records: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """副作用记录 → ``effect_key → status``（``intent`` 归一到 ``pending``）。"""
    return {
        str(key): canonical_status(record.get("status"))
        for key, record in (records or {}).items()
        if str(key)
    }


def reconcilable_step_ids(checkpoints: Iterable[StepCheckpoint]) -> tuple[str, ...]:
    """哪些步骤的在途副作用**可核对**（有 effect_type 且不是 unknown）。"""
    rows: list[str] = []
    for item in unresolved_side_effects(checkpoints):
        # 类型判不出来（UNKNOWN）就没有可靠的核对方式 → 交人工，不猜。
        effect_type = str(getattr(item, "effect_type", "") or "")
        if effect_type and effect_type != "unknown":
            rows.append(str(item.step_id))
    return tuple(sorted(set(rows)))


async def reconcile_pending_effects(
    checkpoints: Iterable[StepCheckpoint],
    records: Mapping[str, Mapping[str, Any]],
    *,
    reconciler: EffectReconciler | None = None,
    idempotency_keys: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], tuple[str, ...]]:
    """核对在途副作用：返回 ``(幂等键 → 判定后的状态, 已核对步骤)``。

    返回的键是**幂等键**（契约 ``plan_resume`` 的 journal 口径）；没有幂等键的步骤
    退回 step_id，保证调用方始终能按同一个键查表。

    判定：
    * ``True`` 已发生 → ``confirmed``（恢复时跳过，不重跑）；
    * ``False`` 明确未发生 → 记为空状态（恢复时按"未执行"重排）；
    * ``None`` / 没有核对器 → 不产出覆盖，保持原来的 ``pending``（恢复时暂停等人工）。
    """
    out: dict[str, str] = {}
    reconciled: list[str] = []
    if reconciler is None:
        return out, ()
    keys = {str(k): str(v) for k, v in (idempotency_keys or {}).items()}
    for checkpoint in unresolved_side_effects(checkpoints):
        step_id = str(checkpoint.step_id)
        serialized_key = keys.get(step_id) or step_id
        record = next(
            (
                item
                for key, item in (records or {}).items()
                if str(key) == serialized_key
                or str(key) == step_id
                or str(item.get("step_id") or "") == step_id
            ),
            None,
        )
        if record is None:
            continue
        if canonical_status(record.get("status")) not in {JOURNAL_PENDING, JOURNAL_UNCERTAIN}:
            continue
        try:
            applied = await reconciler.effect_applied(record)
        except Exception as exc:  # noqa: BLE001 - 核对失败按"无法判定"处理
            logger.debug("[recovery] 副作用核对失败 step={}: {}", step_id[:24], str(exc)[:120])
            applied = None
        reconciled.append(step_id)
        if applied is True:
            out[serialized_key] = JOURNAL_CONFIRMED
        elif applied is False:
            out[serialized_key] = ""
    return out, tuple(sorted(set(reconciled)))


async def load_dependency_results(
    refs: Mapping[str, Any],
    *,
    user_id: str,
    budget: LoadBudget | None = None,
) -> tuple[DependencyLoad, ...]:
    """按引用 + 预算重新加载依赖结果（方案 §5 第 7 步 / §1.4）。

    过期 / 不可用 / 完整性失败都给出**明确错误码**，绝不静默当空上下文。
    """
    from app.services.result_store import get_result_store

    store = get_result_store()
    loads: list[DependencyLoad] = []
    for step_id, ref in (refs or {}).items():
        if not isinstance(ref, dict) or not ref:
            continue
        try:
            resolution, bounded = await store.load_bounded(
                ref, user_id=user_id, budget=budget, strict_owner=True
            )
        except Exception as exc:  # noqa: BLE001 - 引用不可用必须显式报告
            code = str(getattr(exc, "code", "") or "RESULT_REF_UNAVAILABLE")
            loads.append(
                DependencyLoad(
                    step_id=str(step_id),
                    error_code=code,
                    message=str(getattr(exc, "message", "") or "依赖结果引用不可用")[:200],
                )
            )
            continue
        loads.append(
            DependencyLoad(
                step_id=str(step_id),
                body=dict(bounded.body),
                truncated=bool(bounded.truncated or resolution.degraded),
                message=str(resolution.note or "")[:200],
            )
        )
    return tuple(loads)


def dependency_refs_from_checkpoints(
    checkpoints: Iterable[StepCheckpoint],
) -> dict[str, dict[str, Any]]:
    """从检查点里取出"已跳过步骤"的结果引用（恢复时下游要按引用读）。"""
    refs: dict[str, dict[str, Any]] = {}
    for item in checkpoints or ():
        if isinstance(item.result_ref, dict) and item.result_ref:
            refs[str(item.step_id)] = dict(item.result_ref)
    return refs


class JobRecoveryService:
    """方案 §5 的只读核对服务：读状态/检查点/日志 → 出一份恢复计划。"""

    def __init__(
        self,
        *,
        journal_reader: Callable[[str], Awaitable[tuple[dict[str, dict[str, Any]], bool]]] | None = None,
        reconciler: EffectReconciler | None = None,
        clock: Any = None,
    ) -> None:
        self._journal_reader = journal_reader or _default_journal
        self._reconciler = reconciler
        self._clock = clock or time.time

    async def plan(
        self,
        *,
        job_id: str,
        job_plan_revision: int = 1,
        checkpoints: Iterable[StepCheckpoint] = (),
        idempotency_keys: Mapping[str, str] | None = None,
        unfinished_step_ids: Iterable[str] | None = None,
        load_dependencies: bool = False,
        user_id: str = "",
        budget: LoadBudget | None = None,
    ) -> RecoveryReport:
        """执行恢复流程的第 1–7 步（第 1 步的 Job State 由调用方提供）。"""
        rows = list(checkpoints or ())
        records, journal_available = await self._journal_reader(str(job_id or ""))
        statuses = journal_statuses(records)
        keys = _keys_from_checkpoints(rows, idempotency_keys)
        # 可核对清单与状态覆盖都必须按**幂等键**表达：契约 ``plan_resume`` 是按
        # ``step_id → idempotency_key → journal`` 查表的，用 step_id 当键会查不中。
        reconcilable = tuple(
            keys.get(step_id, step_id) for step_id in reconcilable_step_ids(rows)
        )
        overrides, reconciled = await reconcile_pending_effects(
            rows, records, reconciler=self._reconciler, idempotency_keys=keys
        )
        merged = {**statuses, **overrides}
        plan = plan_resume(
            job_id=str(job_id or ""),
            job_plan_revision=int(job_plan_revision or 1),
            checkpoints=rows,
            journal=merged,
            idempotency_keys=keys,
            reconcilable=reconcilable,
            unfinished_step_ids=unfinished_step_ids,
        )
        dependencies: tuple[DependencyLoad, ...] = ()
        if load_dependencies:
            refs = {
                str(step.step_id): dict(step.result_ref)
                for step in plan.steps
                if step.result_ref
            }
            dependencies = await load_dependency_results(refs, user_id=user_id, budget=budget)
        return RecoveryReport(
            plan=plan,
            journal_available=journal_available,
            reconciled_step_ids=reconciled,
            dependencies=dependencies,
            generated_at=float(self._clock()),
        )

    async def plan_for_job(
        self,
        job: Any,
        *,
        store: Any = None,
        load_dependencies: bool = False,
        budget: LoadBudget | None = None,
    ) -> RecoveryReport:
        """便捷入口：直接从内核 ``Job`` 出恢复计划（读检查点 + 副作用日志）。"""
        from app.services.step_checkpoint import load_checkpoints

        job_id = str(getattr(job, "job_id", "") or "")
        routing = getattr(job, "routing", None) if isinstance(getattr(job, "routing", None), dict) else {}
        checkpoints = await load_checkpoints(job_id, store=store)
        keys = {
            str(node.id): str(node.idempotency_key or "")
            for node in (getattr(job, "nodes", None) or [])
            if str(getattr(node, "idempotency_key", "") or "")
        }
        return await self.plan(
            job_id=job_id,
            job_plan_revision=int(routing.get("plan_revision") or 1),
            checkpoints=checkpoints,
            idempotency_keys=keys,
            load_dependencies=load_dependencies,
            user_id=str(getattr(job, "user_id", "") or ""),
            budget=budget,
        )


def _keys_from_checkpoints(
    checkpoints: Iterable[StepCheckpoint],
    explicit: Mapping[str, str] | None,
) -> dict[str, str]:
    """``step_id → idempotency_key``：显式声明优先，其次检查点上记的键。"""
    keys = {str(k): str(v) for k, v in (explicit or {}).items() if str(v)}
    for item in checkpoints or ():
        step_id = str(item.step_id)
        if step_id in keys:
            continue
        key = str(getattr(item, "idempotency_key", "") or "")
        if key:
            keys[step_id] = key
    return keys


def apply_plan_to_job(job: Any, plan: ResumePlan) -> dict[str, Any]:
    """把恢复计划映射成**给既有恢复链路看的**红绿灯（不执行任何重排）。

    返回 ``{"resume_allowed", "skip_step_ids", "reschedule_step_ids", "paused_step_ids"}``；
    跳过/暂停的判定沿用既有 ``TaskStatus`` 语义，避免再发明一套状态。
    """
    from app.agents.orchestration.models import TaskStatus

    skipped = set(plan.skip_step_ids)
    paused = set(plan.paused_step_ids) | set(plan.reconcile_step_ids)
    for node in getattr(job, "nodes", None) or []:
        node_id = str(getattr(node, "id", "") or "")
        if node_id in skipped:
            node.status = TaskStatus.COMPLETED
            node.effect_status = "committed"
            metadata = dict(getattr(node, "metadata", None) or {})
            metadata["recovery"] = "effect_already_committed"
            node.metadata = metadata
        elif node_id in paused:
            node.effect_status = str(node.effect_status or JOURNAL_UNCERTAIN)
    return {
        "resume_allowed": plan.resume_allowed,
        "skip_step_ids": list(plan.skip_step_ids),
        "reschedule_step_ids": list(plan.reschedule_step_ids),
        "paused_step_ids": list(plan.paused_step_ids),
        "reconcile_step_ids": list(plan.reconcile_step_ids),
        "stale_step_ids": list(plan.stale_step_ids),
        "reason_code": plan.reason_code,
    }


def describe_action(action: str) -> str:
    """动作 → 人类可读说明（日志/接口文案用）。"""
    return {
        RecoveryAction.SKIP_CONFIRMED.value: "副作用已确认执行，跳过以免重复",
        RecoveryAction.RESUME.value: "明确未执行，可重新调度",
        RecoveryAction.RECONCILE_PENDING.value: "副作用在途，先核对实际状态",
        RecoveryAction.PAUSE_FOR_HUMAN.value: "副作用状态不确定，暂停等人工确认",
    }.get(str(action or ""), "未知动作")


def summarize_report(report: RecoveryReport) -> str:
    """一行摘要（日志用，不含正文）。"""
    plan = report.plan
    return (
        f"job={plan.job_id[:12]} decision={plan.decision} "
        f"skip={len(plan.skip_step_ids)} resume={len(plan.reschedule_step_ids)} "
        f"reconcile={len(plan.reconcile_step_ids)} paused={len(plan.paused_step_ids)} "
        f"journal={'ok' if report.journal_available else 'unavailable'}"
    )


__all__ = [
    "DependencyLoad",
    "EffectReconciler",
    "JobRecoveryService",
    "RecoveryReport",
    "apply_plan_to_job",
    "dependency_refs_from_checkpoints",
    "describe_action",
    "journal_statuses",
    "load_dependency_results",
    "reconcilable_step_ids",
    "reconcile_pending_effects",
    "summarize_report",
]
