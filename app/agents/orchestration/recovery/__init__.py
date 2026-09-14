"""恢复与重规划（**不是普通执行流程**）。

放在一起的理由
不是"名字像"，而是这些入口共有一套约束——熔断、幂等、审计、证据链：

| 关注点 | 模块 |
| --- | --- |
| 任务恢复（续跑未完成的 Job/Step） | :mod:`~app.agents.orchestration.recovery.job_recovery_service` |
| 副作用恢复（Effect Journal 判定与对账） | :mod:`~app.agents.orchestration.recovery.effect_journal_recovery` |
| 失败任务恢复 | :mod:`~app.agents.orchestration.recovery.failed_job_recovery_service` |
| 失败任务重规划 | :mod:`~app.agents.orchestration.recovery.failed_job_replan_service` |
| 逻辑计划续跑 | :mod:`~app.agents.orchestration.recovery.logical_plan_replan_service` |
| 重规划证据 | :mod:`~app.agents.orchestration.recovery.replan_evidence_service` |
| 恢复策略（该不该重规划） | :mod:`~app.agents.orchestration.recovery.replan_policy` |

**为什么要单独成包**：这些入口只在"出了事"的时候被调用，却和正常执行共享 Job/Step
状态。混在根目录里，读代码的人分不清"这条路径会不会被正常流程走到"，于是熔断与幂等
很难收紧。成包之后，`orchestration` 根目录只剩常规入口与少量协调器。

本 `__init__` 只做**同包聚合再导出**；需要更细的名字（判定函数、报告形状）请直接从
对应子模块导入——真实来源永远比门面清楚。
"""

from app.agents.orchestration.recovery import effect_journal_recovery
from app.agents.orchestration.recovery.effect_journal_recovery import (
    RecoveryOutcome,
    StepRecoveryDecision,
    decide_recovery,
    is_enabled as effect_journal_enabled,
)
from app.agents.orchestration.recovery.failed_job_recovery_service import (
    FailedJobRecoveryService,
)
from app.agents.orchestration.recovery.failed_job_replan_service import FailedJobReplanService
from app.agents.orchestration.recovery.job_recovery_service import (
    JobRecoveryService,
    RecoveryReport,
    load_dependency_results,
    summarize_report,
)
from app.agents.orchestration.recovery.logical_plan_replan_service import (
    LogicalPlanReplanService,
)
from app.agents.orchestration.recovery.replan_evidence_service import ReplanEvidenceService
from app.agents.orchestration.recovery.replan_policy import (
    decide_failed_job_replan,
    decide_logical_plan_replan,
)

__all__ = [
    "FailedJobRecoveryService",
    "FailedJobReplanService",
    "JobRecoveryService",
    "LogicalPlanReplanService",
    "RecoveryOutcome",
    "RecoveryReport",
    "ReplanEvidenceService",
    "StepRecoveryDecision",
    "decide_failed_job_replan",
    "decide_logical_plan_replan",
    "decide_recovery",
    "effect_journal_enabled",
    "effect_journal_recovery",
    "load_dependency_results",
    "summarize_report",
]
