"""阶段 3：把 Effect Journal 恢复计划接进既有恢复链路（``EFFECT_JOURNAL_RECOVERY_V2``）。

分工：

* **契约决策面**（``lumi_contracts.persistence.effect_recovery.plan_recovery``）只回答
  "按副作用状态该跳过/暂停/重排"，不认识业务；
* **本模块**是应用侧的唯一收口：从既有 Effect Journal（
  :mod:`app.agents.orchestration.effects`，不新建第二套日志）读取状态，再叠加
  "哪些步骤允许自动重放"的 App 级红绿灯：

  1. 日志里 ``confirmed`` → 跳过（重跑会造成重复副作用）；
  2. 日志里 ``uncertain`` / ``intent`` → 暂停等人工确认；
  3. 日志不可用（数据库不可读）→ **fail-closed** 暂停，绝不默认"没记录=没执行"；
  4. 高风险步骤（删除/提交/发布/外发/支付等，或已标记需审批）→ 永不自动重放；
  5. 其余可重排步骤里，只有**只读步骤**与**带 idempotency_key 的幂等步骤**才自动恢复；
     有写声明却没有幂等键的步骤同样暂停（重放不安全）。

因此"自动恢复"只会落在能被安全重放的步骤上；只要有一个步骤需要人工，整单
``resume_allowed=False``——避免"部分暂停、整体继续"导致脏副作用。

本模块**只读**：不写库、不改 Job。调用方（
:class:`app.agents.orchestration.failed_job_recovery_service.FailedJobRecoveryService`）
据返回值决定是否继续自动恢复，以及哪些已确认步骤应标记为跳过。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from lumi_contracts.persistence.effect_recovery import (
    JOURNAL_CONFIRMED,
    JOURNAL_INTENT,
    JOURNAL_UNCERTAIN,
    RecoveryAction,
    plan_recovery,
)

from app.agents.orchestration.models import Job, TaskNode, TaskStatus
from app.agents.orchestration.safety import is_effectful

#: 已结束的步骤不再需要恢复（取消/跳过是有意为之，不是"未完成"）。
FINISHED_NODE_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.SKIPPED}
)

#: 高风险动作词（工具名/动作/模式/显式动作参数命中即视为不可自动重放）。
#: 覆盖"删除/提交/发布/外发/部署/支付"这些一旦重复就对外可见或不可逆的动作。
HIGH_RISK_HINTS: tuple[str, ...] = (
    "delete", "remove", "drop", "unlink", "commit", "publish", "push",
    "send", "submit", "deploy", "release", "merge", "install", "uninstall",
    "revoke", "transfer", "pay", "purchase", "terminate", "rollback",
    "删除", "移除", "提交", "发布", "推送", "上传", "发送", "部署", "支付", "转账", "安装", "回滚",
)

#: 明确只读的声明：命中时即使任务名里带"提交/发送"等词也不判高风险（避免误暂停）。
READ_ONLY_HINTS: tuple[str, ...] = (
    "read", "list", "stat", "search", "glob", "grep", "inspect", "query", "预览", "读取", "查看",
)

#: 原因码（前三个与契约侧 ``plan_recovery`` 对齐，其余是本模块的红绿灯）。
REASON_SKIP_CONFIRMED = "EFFECT_ALREADY_COMMITTED"
REASON_UNCERTAIN = "EFFECT_UNCERTAIN"
REASON_IN_FLIGHT = "EFFECT_IN_FLIGHT"
REASON_JOURNAL_UNAVAILABLE = "EFFECT_JOURNAL_UNAVAILABLE"
REASON_HIGH_RISK = "HIGH_RISK_EFFECT_REQUIRES_HUMAN"
REASON_NOT_IDEMPOTENT = "EFFECT_WITHOUT_IDEMPOTENCY_KEY"
REASON_IDEMPOTENT_RESUME = "IDEMPOTENT_EFFECT_NOT_STARTED"
REASON_READ_ONLY_RESUME = "READ_ONLY_STEP_NOT_STARTED"


@dataclass(frozen=True, slots=True)
class StepRecoveryDecision:
    """单个步骤的最终恢复决策（App 级红绿灯后的结论）。"""

    step_id: str
    action: str
    reason_code: str = ""
    effect_status: str = ""
    idempotency_key: str = ""
    requires_human: bool = False
    auto_resume: bool = False
    high_risk: bool = False
    read_only: bool = False
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        # 只出审计字段：不含参数/原始输入，避免把任务正文带进 routing。
        return {
            "step_id": self.step_id,
            "action": self.action,
            "reason_code": self.reason_code,
            "effect_status": self.effect_status,
            "requires_human": self.requires_human,
            "auto_resume": self.auto_resume,
            "high_risk": self.high_risk,
            "read_only": self.read_only,
        }


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """整单恢复结论（``resume_allowed`` 是唯一红灯）。"""

    steps: tuple[StepRecoveryDecision, ...] = field(default_factory=tuple)
    journal_available: bool = True

    @property
    def skip_step_ids(self) -> tuple[str, ...]:
        return tuple(s.step_id for s in self.steps if s.action == RecoveryAction.SKIP_CONFIRMED.value)

    @property
    def resumable_step_ids(self) -> tuple[str, ...]:
        return tuple(s.step_id for s in self.steps if s.auto_resume)

    @property
    def paused_step_ids(self) -> tuple[str, ...]:
        return tuple(s.step_id for s in self.steps if s.requires_human)

    @property
    def requires_human(self) -> bool:
        return bool(self.paused_step_ids)

    @property
    def resume_allowed(self) -> bool:
        return not self.requires_human

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return tuple(s.reason_code for s in self.steps if s.reason_code)

    def as_dict(self) -> dict[str, Any]:
        return {
            "resume_allowed": self.resume_allowed,
            "requires_human": self.requires_human,
            "journal_available": self.journal_available,
            "skip_step_ids": list(self.skip_step_ids),
            "resumable_step_ids": list(self.resumable_step_ids),
            "paused_step_ids": list(self.paused_step_ids),
            "reason_codes": list(self.reason_codes),
            "steps": [s.as_dict() for s in self.steps],
        }


# ── 步骤画像（纯函数，可单测）────────────────────────────────


def _tool_of(node: TaskNode) -> str:
    params = node.params or {}
    return str(params.get("preferred_tool") or params.get("tool") or node.agent or "").strip()


def _high_risk_material(node: TaskNode) -> str:
    """拼出用于高风险判定的**声明文本**（工具名/动作/模式/显式动作参数）。"""
    params = node.params or {}
    pieces = [_tool_of(node), str(node.name or ""), str(node.agent or "")]
    for key in ("action", "operation", "op", "mode", "method", "intent", "capability", "kind"):
        value = params.get(key)
        if isinstance(value, (str, int, float)):
            pieces.append(str(value))
    arguments = params.get("arguments")
    if isinstance(arguments, dict):
        for key in ("action", "operation", "op", "mode", "command", "method"):
            value = arguments.get(key)
            if isinstance(value, (str, int, float)):
                pieces.append(str(value))
    return " ".join(piece.lower() for piece in pieces if piece)


def is_read_only_step(node: TaskNode) -> bool:
    """只读步骤：没有任何写资源声明，也没有幂等键 → 重放不会产生副作用。"""
    return not is_effectful(node)


def is_high_risk_step(node: TaskNode) -> bool:
    """高风险步骤：删除/提交/发布/外发等，或已被要求人工审批。

    只读声明优先：显式只读工具（``office_doc_read`` 等）不会因为任务名里带
    "提交/发送"这类词被误判成高风险。
    """
    if bool(getattr(node, "approval", False)):
        return True
    material = _high_risk_material(node)
    if not material:
        return False
    if any(hint in material for hint in READ_ONLY_HINTS):
        return False
    return any(hint in material for hint in HIGH_RISK_HINTS)


def is_idempotent_step(node: TaskNode) -> bool:
    """幂等步骤：副作用步骤且声明了 ``idempotency_key``（Effect Journal 的键）。"""
    return bool(str(node.idempotency_key or "").strip())


def unfinished_nodes(job: Job) -> list[TaskNode]:
    """未完成步骤（按原顺序）；已终态/被取消/被跳过的步骤不需要恢复。"""
    return [node for node in (job.nodes or []) if node.status not in FINISHED_NODE_STATUSES]


def recovery_inputs(job: Job) -> tuple[list[str], dict[str, str]]:
    """未完成步骤 id + ``step_id → idempotency_key``（仅副作用步骤带键）。"""
    unfinished: list[str] = []
    keys: dict[str, str] = {}
    for node in unfinished_nodes(job):
        step_id = str(node.id or "")
        if not step_id:
            continue
        unfinished.append(step_id)
        key = str(node.idempotency_key or "").strip()
        if key:
            keys[step_id] = key
    return unfinished, keys


def _node_effect_status(node: TaskNode) -> str:
    """节点上已知的副作用状态 → 日志口径（仅取明确的 committed/uncertain）。

    兜底用途：旧快照可能已带 ``effect_status="committed"`` 但日志记录缺失；此时
    宁可跳过/暂停，也不能当成"没执行过"重放。
    """
    raw = str(node.effect_status or "").strip().lower()
    if raw in {"committed", "confirmed"}:
        return JOURNAL_CONFIRMED
    if raw == "uncertain":
        return JOURNAL_UNCERTAIN
    return ""


def _decision(
    node: TaskNode,
    *,
    action: str,
    reason_code: str,
    effect_status: str = "",
    requires_human: bool = False,
    auto_resume: bool = False,
    message: str = "",
) -> StepRecoveryDecision:
    return StepRecoveryDecision(
        step_id=str(node.id or ""),
        action=action,
        reason_code=reason_code,
        effect_status=effect_status,
        idempotency_key=str(node.idempotency_key or "").strip(),
        requires_human=requires_human,
        auto_resume=auto_resume,
        high_risk=is_high_risk_step(node),
        read_only=is_read_only_step(node),
        message=message,
    )


def _pause_for_journal(node: TaskNode, reason_code: str, message: str, status: str) -> StepRecoveryDecision:
    return _decision(
        node,
        action=RecoveryAction.PAUSE_FOR_HUMAN.value,
        reason_code=reason_code,
        effect_status=status,
        requires_human=True,
        message=message,
    )


def _decide_from_plan(
    node: TaskNode,
    planned_action: str,
    planned_reason: str,
    planned_status: str,
    *,
    journal_available: bool,
) -> StepRecoveryDecision:
    """契约决策 + App 级红绿灯 → 最终决策。"""
    # 日志没有记录时，回落到节点上已知的副作用状态（旧快照兜底）：宁可跳过/暂停。
    status = planned_status or _node_effect_status(node)
    if planned_action == RecoveryAction.SKIP_CONFIRMED.value or status == JOURNAL_CONFIRMED:
        return _decision(
            node,
            action=RecoveryAction.SKIP_CONFIRMED.value,
            reason_code=REASON_SKIP_CONFIRMED,
            effect_status=status or JOURNAL_CONFIRMED,
            message="该步骤的副作用已确认执行，跳过以免重复。",
        )
    if (
        planned_action == RecoveryAction.PAUSE_FOR_HUMAN.value
        or status in {JOURNAL_UNCERTAIN, JOURNAL_INTENT}
    ):
        uncertain = planned_reason == "EFFECT_UNCERTAIN" or status == JOURNAL_UNCERTAIN
        return _pause_for_journal(
            node,
            REASON_UNCERTAIN if uncertain else REASON_IN_FLIGHT,
            "该步骤的副作用状态不确定，已暂停自动恢复，请人工确认后再继续。"
            if uncertain
            else "该步骤的副作用已开始但未确认，已暂停自动恢复，请人工确认后再继续。",
            status or (JOURNAL_UNCERTAIN if uncertain else JOURNAL_INTENT),
        )
    # 契约判定为"未执行、可重排"：继续走 App 级红绿灯。
    if not journal_available:
        # fail-closed：日志读不到时不允许把"没有记录"当成"没有执行"。
        return _pause_for_journal(
            node,
            REASON_JOURNAL_UNAVAILABLE,
            "副作用日志不可用，无法判断是否已执行，已暂停自动恢复。",
            "",
        )
    if is_high_risk_step(node):
        return _pause_for_journal(
            node,
            REASON_HIGH_RISK,
            "该步骤属于高风险动作（删除/提交/发布等），不会自动重放，请人工确认。",
            "",
        )
    if not is_effectful(node):
        return _decision(
            node,
            action=RecoveryAction.RESUME.value,
            reason_code=REASON_READ_ONLY_RESUME,
            auto_resume=True,
            message="只读步骤未执行过副作用，可自动重新调度。",
        )
    if is_idempotent_step(node):
        return _decision(
            node,
            action=RecoveryAction.RESUME.value,
            reason_code=REASON_IDEMPOTENT_RESUME,
            auto_resume=True,
            message="该步骤带幂等键且未开始过副作用，可安全自动重试。",
        )
    return _pause_for_journal(
        node,
        REASON_NOT_IDEMPOTENT,
        "该步骤有写副作用但没有幂等键，重放不安全，已暂停自动恢复。",
        "",
    )


def decide_recovery(
    job: Job,
    journal: Mapping[str, str] | None = None,
    *,
    journal_available: bool = True,
) -> RecoveryOutcome:
    """纯决策：契约 ``plan_recovery`` + App 级"可否自动重放"红绿灯。"""
    unfinished, keys = recovery_inputs(job)
    nodes = {str(node.id or ""): node for node in unfinished_nodes(job)}
    planned = plan_recovery(unfinished, journal, idempotency_keys=keys)
    decisions: list[StepRecoveryDecision] = []
    for step in planned.steps:
        node = nodes.get(step.step_id)
        if node is None:
            continue
        decisions.append(
            _decide_from_plan(
                node,
                step.action,
                step.reason_code,
                step.effect_status,
                journal_available=journal_available,
            )
        )
    return RecoveryOutcome(steps=tuple(decisions), journal_available=journal_available)


# ── 既有 Effect Journal 读取 + 异步入口 ──────────────────────


async def _default_reader(key: str) -> dict[str, Any] | None:
    from app.agents.orchestration.effects import get_effect

    return await get_effect(key)


async def load_journal_statuses(
    keys: Iterable[str],
    *,
    reader: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None,
) -> tuple[dict[str, str], bool]:
    """按幂等键读取 Effect Journal：``(key → status, 是否全部可读)``。

    读取失败（日志不可用）返回 ``available=False``，由调用方 fail-closed 暂停。
    """
    read = reader or _default_reader
    statuses: dict[str, str] = {}
    available = True
    for key in sorted({str(item or "").strip() for item in keys}):
        if not key:
            continue
        try:
            record = await read(key)
        except Exception:  # noqa: BLE001 - 日志不可用不是"没执行"，由 available=False 表达
            available = False
            continue
        if isinstance(record, dict):
            status = str(record.get("status") or "").strip()
            if status:
                statuses[key] = status
    return statuses, available


async def plan_job_recovery(
    job: Job,
    *,
    reader: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None,
) -> RecoveryOutcome:
    """读取既有 Effect Journal 并给出整单恢复结论（只读，不改 Job）。"""
    _, keys = recovery_inputs(job)
    statuses, available = await load_journal_statuses(keys.values(), reader=reader)
    return decide_recovery(job, statuses, journal_available=available)


def is_enabled() -> bool:
    """``EFFECT_JOURNAL_RECOVERY_V2``（默认关闭；关闭时恢复链路保持既有行为）。"""
    from app.core.feature_flags import feature_enabled

    return feature_enabled("EFFECT_JOURNAL_RECOVERY_V2")


__all__ = [
    "FINISHED_NODE_STATUSES",
    "HIGH_RISK_HINTS",
    "REASON_HIGH_RISK",
    "REASON_IDEMPOTENT_RESUME",
    "REASON_IN_FLIGHT",
    "REASON_JOURNAL_UNAVAILABLE",
    "REASON_NOT_IDEMPOTENT",
    "REASON_READ_ONLY_RESUME",
    "REASON_SKIP_CONFIRMED",
    "REASON_UNCERTAIN",
    "RecoveryOutcome",
    "StepRecoveryDecision",
    "decide_recovery",
    "is_enabled",
    "is_high_risk_step",
    "is_idempotent_step",
    "is_read_only_step",
    "load_journal_statuses",
    "plan_job_recovery",
    "recovery_inputs",
    "unfinished_nodes",
]
