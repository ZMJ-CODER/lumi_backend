"""插件生命周期状态机与资源配额（方案 §4.2 / §4.3）。

两条契约：

* **状态机**：``registered → enabled ⇄ disabled``、``enabled → upgrading → enabled``、
  ``enabled → uninstalling → uninstalled``、``enabled → crashed → disabled``；
  非法迁移直接报错（不要让插件"从 uninstalled 又跑起来"）。
* **运行中 Job 的处理策略按操作分级**（不一刀切）：``disable`` 复用预检阻断、
  ``upgrade`` 走 drain、``uninstall`` 标 uncertain、``crash`` 靠幂等键 + 熔断。
* **配额两级**：内置插件软约束、第三方硬约束；**输出超限转 Artifact 引用（不报错）**，
  **时间超限杀 Worker + ``PLUGIN_RESOURCE_EXCEEDED``**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class PluginState(StrEnum):
    REGISTERED = "registered"
    ENABLED = "enabled"
    DISABLED = "disabled"
    UPGRADING = "upgrading"
    UNINSTALLING = "uninstalling"
    UNINSTALLED = "uninstalled"
    CRASHED = "crashed"


#: 合法迁移表（不在表内的迁移一律拒绝）。
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    PluginState.REGISTERED.value: frozenset({PluginState.ENABLED.value, PluginState.UNINSTALLED.value}),
    PluginState.ENABLED.value: frozenset({
        PluginState.DISABLED.value, PluginState.UPGRADING.value,
        PluginState.UNINSTALLING.value, PluginState.CRASHED.value,
    }),
    PluginState.DISABLED.value: frozenset({PluginState.ENABLED.value, PluginState.UNINSTALLING.value}),
    PluginState.UPGRADING.value: frozenset({PluginState.ENABLED.value, PluginState.CRASHED.value}),
    PluginState.UNINSTALLING.value: frozenset({PluginState.UNINSTALLED.value, PluginState.CRASHED.value}),
    PluginState.CRASHED.value: frozenset({PluginState.DISABLED.value, PluginState.ENABLED.value}),
    PluginState.UNINSTALLED.value: frozenset(),
}

#: 终态（不可再迁移）。
TERMINAL_STATES: frozenset[str] = frozenset({PluginState.UNINSTALLED.value})

#: 连续崩溃达到该次数即熔断（自动 disabled + 告警）。
CRASH_CIRCUIT_THRESHOLD = 3


def can_transition(current: str, target: str) -> bool:
    source = str(current or "").strip()
    destination = str(target or "").strip()
    if source == destination:
        return True
    return destination in ALLOWED_TRANSITIONS.get(source, frozenset())


def transition(current: str, target: str) -> str:
    """执行迁移；非法迁移抛 ``ValueError``（调用方转 UnifiedError）。"""
    if not can_transition(current, target):
        raise ValueError(f"非法插件状态迁移：{current} → {target}")
    return str(target or "").strip()


@dataclass(frozen=True, slots=True)
class OperationPolicy:
    """某个生命周期操作对"运行中 Job"的处理策略。"""

    operation: str
    inflight: str
    new_steps: str
    reason_code: str = ""
    requires_drain: bool = False
    marks_uncertain: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "inflight": self.inflight,
            "new_steps": self.new_steps,
            "reason_code": self.reason_code,
            "requires_drain": self.requires_drain,
            "marks_uncertain": self.marks_uncertain,
        }


#: 操作 → 运行中 Job 策略（方案 §4.2 表）。
OPERATION_POLICIES: dict[str, OperationPolicy] = {
    "disable": OperationPolicy(
        operation="disable",
        inflight="finish",              # 已开始的调用不回收
        new_steps="preflight_block",    # 未开始的 step 走预检失败
        reason_code="CAPABILITY_UNAVAILABLE",
    ),
    "upgrade": OperationPolicy(
        operation="upgrade",
        inflight="finish",              # drain：旧 Worker 跑完当前 Job
        new_steps="use_new_version",
        reason_code="PLUGIN_DRAINING",
        requires_drain=True,
    ),
    "uninstall": OperationPolicy(
        operation="uninstall",
        inflight="uncertain",           # 在途副作用标 uncertain，恢复时查 Effect Journal
        new_steps="preflight_block",
        reason_code="PLUGIN_UNINSTALLED",
        marks_uncertain=True,
    ),
    "crash": OperationPolicy(
        operation="crash",
        inflight="restart",             # 重启 + 幂等键去重
        new_steps="normal",
        reason_code="PLUGIN_CRASHED",
    ),
}


def operation_policy(operation: str) -> OperationPolicy:
    key = str(operation or "").strip().casefold()
    if key not in OPERATION_POLICIES:
        raise ValueError(f"未知插件操作：{operation}")
    return OPERATION_POLICIES[key]


def should_circuit_break(consecutive_crashes: int, *, threshold: int = CRASH_CIRCUIT_THRESHOLD) -> bool:
    """连续崩溃达到阈值 → 熔断（自动 ``disabled`` + 告警）。"""
    try:
        count = int(consecutive_crashes)
    except (TypeError, ValueError):
        return False
    return count >= max(1, int(threshold))


class PluginQuota(BaseModel):
    """Manifest 声明的资源配额（内置软约束 / 第三方容器硬约束）。"""

    model_config = ConfigDict(extra="ignore")

    cpu_limit: float = 1.0
    memory_mb: int = 512
    timeout_seconds: int = 30
    max_output_bytes: int = 10 * 1024 * 1024
    network_egress: str = "allowlist"
    network_rate_limit: str = "10/min"


class QuotaAction(StrEnum):
    NONE = "NONE"
    ARTIFACT_REF = "ARTIFACT_REF"      # 输出超限：转 Artifact + result_ref（不报错）
    KILL_WORKER = "KILL_WORKER"        # 时间/CPU 超限：杀 Worker + PLUGIN_RESOURCE_EXCEEDED
    WARN = "WARN"                      # 内置插件的软约束
    REFUSED = "REFUSED"                # 需要硬终止却只有协作式：拒绝执行（不假装跑过）


@dataclass(frozen=True, slots=True)
class QuotaDecision:
    action: str = QuotaAction.NONE.value
    error_code: str = ""
    exceeded: tuple[str, ...] = field(default_factory=tuple)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.action == QuotaAction.KILL_WORKER.value

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "error_code": self.error_code,
            "exceeded": list(self.exceeded),
            **self.detail,
        }


def enforce_quota(
    quota: PluginQuota,
    *,
    output_bytes: int = 0,
    elapsed_seconds: float = 0.0,
    hard_limits: bool = True,
) -> QuotaDecision:
    """按配额判定动作。``hard_limits=False``（内置插件）只给软约束（WARN）。

    * 输出超限 → ``ARTIFACT_REF``（正文进产物，事件只带引用，**不算失败**）；
    * 时间超限 → ``KILL_WORKER`` + ``PLUGIN_RESOURCE_EXCEEDED``。
    """
    exceeded: list[str] = []
    if output_bytes and int(output_bytes) > int(quota.max_output_bytes):
        exceeded.append("max_output_bytes")
    timed_out = bool(elapsed_seconds) and float(elapsed_seconds) > float(quota.timeout_seconds)
    if timed_out:
        exceeded.append("timeout_seconds")
    if not exceeded:
        return QuotaDecision()

    if timed_out:
        if not hard_limits:
            return QuotaDecision(
                action=QuotaAction.WARN.value,
                error_code="PLUGIN_RESOURCE_EXCEEDED",
                exceeded=tuple(exceeded),
                detail={"limit_seconds": quota.timeout_seconds, "elapsed_seconds": float(elapsed_seconds)},
            )
        return QuotaDecision(
            action=QuotaAction.KILL_WORKER.value,
            error_code="PLUGIN_RESOURCE_EXCEEDED",
            exceeded=tuple(exceeded),
            detail={"limit_seconds": quota.timeout_seconds, "elapsed_seconds": float(elapsed_seconds)},
        )
    # 只有输出超限：转引用，不报错
    return QuotaDecision(
        action=QuotaAction.ARTIFACT_REF.value,
        exceeded=tuple(exceeded),
        detail={"limit_bytes": quota.max_output_bytes, "output_bytes": int(output_bytes)},
    )


__all__ = [
    "ALLOWED_TRANSITIONS",
    "CRASH_CIRCUIT_THRESHOLD",
    "OPERATION_POLICIES",
    "TERMINAL_STATES",
    "OperationPolicy",
    "PluginQuota",
    "PluginState",
    "QuotaAction",
    "QuotaDecision",
    "can_transition",
    "enforce_quota",
    "operation_policy",
    "should_circuit_break",
    "transition",
]
