"""阶段 4 收口：**插件执行边界**契约（归类 / 终止方式 / 配额状态四级）。

三个纯函数（不读配置、不碰存储、不执行任何东西），是整个"声明 → 执行"链路的唯一判定处：

1. :func:`classify_execution` —— 一次执行**只**在同时携带 ``plugin_id`` + manifest +
   ``quota_spec`` 时才属于插件；否则显式归为 ``builtin``（系统默认上限）。
   内置执行**永不被**当成"受插件配额治理"。
2. :func:`decide_termination` —— 执行类 + 终止原因 → ``cooperative`` | ``forced`` |
   ``refused``：进程内协作取消只允许用于**超时通知 / 正常取消 / 用户停止**；
   需要硬终止（CPU 超限、内存超限、死循环、失控子进程）的必须走独立进程/容器，
   拿不到强杀能力时**拒绝**（返回阻断码），绝不"悄悄按协作式跑"。
3. :func:`quota_status_levels` —— 诚实状态四级 ``declared`` / ``observed`` /
   ``wired`` / ``enforced``；协作式**不算** ``enforced``。

边界：本模块只定义**判定**。真正取 Worker（``PluginManager.worker_for``）与记录执行证据
在 ``app.services.plugins.boundary``；Worker 真杀进程在 ``app.services.plugins.quota``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

#: 插件执行拿不到 Worker（未安装 / 未交出执行边界）时的稳定阻断码。
PLUGIN_WORKER_UNAVAILABLE = "PLUGIN_WORKER_UNAVAILABLE"
#: 插件声明了配额、但本次执行无法真执行它（只有协作式取消等）时的稳定阻断码。
PLUGIN_QUOTA_NOT_ENFORCED = "PLUGIN_QUOTA_NOT_ENFORCED"

#: 内置执行的系统默认上限（**不是**插件配额，永不进插件治理）。
SYSTEM_DEFAULT_LIMITS: dict[str, Any] = {
    "wall_seconds": 30.0,
    "memory_mb": 512,
    "cpu_seconds": 60.0,
    "max_output_bytes": 2_000_000,
    "max_concurrency": 0,  # 0 = 不进插件并发门
    "hard_limits": False,
    "governed_by": "system_default",
}


class ExecutionClass(StrEnum):
    """一次执行属于谁。"""

    PLUGIN = "plugin"
    BUILTIN = "builtin"


class TerminationMode(StrEnum):
    """终止方式（明确判定，不允许"默认协作式"）。"""

    COOPERATIVE = "cooperative"  # 进程内协作取消（仅限通知/取消/用户停止）
    FORCED = "forced"            # 独立进程/容器，可强杀
    REFUSED = "refused"          # 需要强杀但拿不到强杀能力 → 阻断


class TerminationReason(StrEnum):
    """为什么要终止（决定协作式够不够）。"""

    TIMEOUT_NOTIFY = "timeout_notify"
    CANCEL = "cancel"
    USER_STOP = "user_stop"
    CPU_LIMIT = "cpu_limit"
    MEMORY_LIMIT = "memory_limit"
    RUNAWAY_LOOP = "runaway_loop"
    RUNAWAY_CHILD_PROCESS = "runaway_child_process"


#: 进程内协作取消**足够**的原因（只有这三个）。
COOPERATIVE_TERMINATION_REASONS: frozenset[str] = frozenset(
    {
        TerminationReason.TIMEOUT_NOTIFY.value,
        TerminationReason.CANCEL.value,
        TerminationReason.USER_STOP.value,
    }
)

#: **必须**强杀的原因（进程内做不做得到都得上独立进程）。
FORCED_TERMINATION_REASONS: frozenset[str] = frozenset(
    {
        TerminationReason.CPU_LIMIT.value,
        TerminationReason.MEMORY_LIMIT.value,
        TerminationReason.RUNAWAY_LOOP.value,
        TerminationReason.RUNAWAY_CHILD_PROCESS.value,
    }
)


def _limits_from_spec(quota_spec: Any) -> dict[str, Any]:
    """插件配额视图 → 执行上限（duck-typed，避免契约层依赖 ``app``）。"""
    quota = getattr(quota_spec, "quota", None)
    return {
        "wall_seconds": float(getattr(quota, "timeout_seconds", 0) or 0),
        "memory_mb": int(getattr(quota, "memory_mb", 0) or 0),
        "cpu_seconds": float(getattr(quota_spec, "cpu_seconds", 0.0) or 0.0),
        "max_output_bytes": int(getattr(quota, "max_output_bytes", 0) or 0),
        "max_concurrency": int(getattr(quota_spec, "max_concurrency", 1) or 1),
        "hard_limits": bool(getattr(quota_spec, "hard_limits", True)),
        "governed_by": "plugin_manifest",
    }


@dataclass(frozen=True, slots=True)
class ExecutionClassification:
    """归类结论（``missing`` 是**显式**缺哪一件边界要件）。"""

    kind: str = ExecutionClass.BUILTIN.value
    plugin_id: str = ""
    plugin_version: str = ""
    missing: tuple[str, ...] = ()
    limits: dict[str, Any] = field(default_factory=dict)
    #: ``quota_spec`` 声明的 plugin_id 与本次执行的 plugin_id 不一致（张冠李戴）。
    mismatched_spec: bool = False
    manifest: Any = None
    quota_spec: Any = None

    @property
    def plugin_governed(self) -> bool:
        """本次执行是否**真的**受插件配额治理。"""
        return self.kind == ExecutionClass.PLUGIN.value

    @property
    def unattributed_plugin(self) -> bool:
        """带了插件身份/配额声明、却没有完整边界（既不能当内置跑，也不能被治理）。"""
        if self.plugin_governed:
            return False
        return bool(self.plugin_id) or self.manifest is not None or self.quota_spec is not None

    @property
    def system_default(self) -> bool:
        return not self.plugin_governed

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "plugin_governed": self.plugin_governed,
            "unattributed_plugin": self.unattributed_plugin,
            "missing": list(self.missing),
            "mismatched_spec": bool(self.mismatched_spec),
            "limits": dict(self.limits),
        }


def classify_execution(
    *,
    plugin_id: Any = "",
    manifest: Any = None,
    quota_spec: Any = None,
) -> ExecutionClassification:
    """**唯一**的执行归类函数：插件执行 = ``plugin_id`` + manifest + ``quota_spec`` 三件齐备。

    * 三件齐备 → :data:`ExecutionClass.PLUGIN`（受插件配额治理）；
    * 缺任意一件 → :data:`ExecutionClass.BUILTIN`，用系统默认上限，
      **不**按插件配额治理；缺件记进 ``missing`` 供上层阻断/排障；
    * 带了 ``plugin_id`` 但缺件 → ``unattributed_plugin=True``（上层必须拒绝，
      既不许当内置跑，也不许假装治理过）。

    ``quota_spec`` 里声明的 ``plugin_id`` 与本次执行不一致时按缺件处理
    （宁可不治理，也不张冠李戴）。
    """
    key = str(plugin_id or "").strip()
    missing: list[str] = []
    if not key:
        missing.append("plugin_id")
    if manifest is None:
        missing.append("manifest")
    spec_plugin_id = ""
    if quota_spec is None:
        missing.append("quota_spec")
    else:
        spec_plugin_id = str(getattr(quota_spec, "plugin_id", "") or "").strip()
    mismatched = bool(spec_plugin_id) and bool(key) and spec_plugin_id != key
    if mismatched:
        missing.append("quota_spec")
    if missing:
        return ExecutionClassification(
            kind=ExecutionClass.BUILTIN.value,
            plugin_id=key,
            missing=tuple(dict.fromkeys(missing)),
            limits=dict(SYSTEM_DEFAULT_LIMITS),
            mismatched_spec=mismatched,
            manifest=manifest,
            quota_spec=quota_spec if not mismatched else None,
        )
    return ExecutionClassification(
        kind=ExecutionClass.PLUGIN.value,
        plugin_id=key,
        plugin_version=str(getattr(quota_spec, "plugin_version", "") or ""),
        missing=(),
        limits=_limits_from_spec(quota_spec),
        manifest=manifest,
        quota_spec=quota_spec,
    )


def forced_termination_reasons(execution: ExecutionClassification) -> tuple[str, ...]:
    """该执行**必须**具备强杀能力的原因（空 = 协作式就够）。"""
    if not execution.plugin_governed or not execution.limits.get("hard_limits"):
        return ()
    reasons: list[str] = []
    if float(execution.limits.get("cpu_seconds") or 0.0) > 0:
        reasons.append(TerminationReason.CPU_LIMIT.value)
    if int(execution.limits.get("memory_mb") or 0) > 0:
        reasons.append(TerminationReason.MEMORY_LIMIT.value)
    if float(execution.limits.get("wall_seconds") or 0.0) > 0:
        reasons.append(TerminationReason.RUNAWAY_LOOP.value)
    return tuple(reasons)


def requires_forced_termination(reason: Any) -> bool:
    """该终止原因是否**必须**走独立进程/容器（协作式不够）。"""
    return str(reason or "").strip().casefold() in FORCED_TERMINATION_REASONS


def _default_reason(execution: ExecutionClassification) -> str:
    reasons = forced_termination_reasons(execution)
    if reasons:
        # 墙钟超限（死循环）是默认要防的那一个：它必须真杀进程。
        preferred = (
            TerminationReason.RUNAWAY_LOOP.value,
            TerminationReason.CPU_LIMIT.value,
            TerminationReason.MEMORY_LIMIT.value,
        )
        for item in preferred:
            if item in reasons:
                return item
        return reasons[0]
    return TerminationReason.TIMEOUT_NOTIFY.value


@dataclass(frozen=True, slots=True)
class TerminationDecision:
    """一次执行的终止方式判定（``refused`` 时必须带阻断码）。"""

    mode: str = TerminationMode.COOPERATIVE.value
    reason: str = TerminationReason.TIMEOUT_NOTIFY.value
    error_code: str = ""
    detail: str = ""
    hard_termination_required: bool = False
    worker_available: bool = True
    forced_available: bool = True

    @property
    def refused(self) -> bool:
        return self.mode == TerminationMode.REFUSED.value

    @property
    def cooperative(self) -> bool:
        return self.mode == TerminationMode.COOPERATIVE.value

    @property
    def forced(self) -> bool:
        return self.mode == TerminationMode.FORCED.value

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "error_code": self.error_code,
            "detail": self.detail,
            "hard_termination_required": bool(self.hard_termination_required),
            "worker_available": bool(self.worker_available),
            "forced_available": bool(self.forced_available),
        }


def decide_termination(
    *,
    execution: ExecutionClassification,
    reason: Any = "",
    worker_available: bool = True,
    forced_available: bool = True,
) -> TerminationDecision:
    """执行类 + 终止原因 → ``cooperative`` | ``forced`` | ``refused``（纯判定）。

    规则：

    * 内置执行：系统默认上限，**不**受插件配额治理 → ``cooperative``；
    * 插件身份不完整（``unattributed_plugin``）→ ``refused`` +
      ``PLUGIN_QUOTA_NOT_ENFORCED``（不许当内置跑，也不许假装治理过）；
    * 插件 + 通知/取消/用户停止 → ``cooperative``（进程内协作取消合法）；
    * 插件 + 需要硬终止：
      - 没有 Worker → ``refused`` + ``PLUGIN_WORKER_UNAVAILABLE``；
      - 有 Worker 但拿不到强杀能力（只有进程内）→ ``refused`` +
        ``PLUGIN_QUOTA_NOT_ENFORCED``；
      - 否则 → ``forced``（必须走 ``run_process``）。
    """
    key = str(reason or "").strip().casefold() or _default_reason(execution)
    if execution.unattributed_plugin:
        return TerminationDecision(
            mode=TerminationMode.REFUSED.value,
            reason=key,
            error_code=PLUGIN_QUOTA_NOT_ENFORCED,
            detail=(
                "插件身份/配额声明不完整（缺 "
                + "、".join(execution.missing or ("quota_spec",))
                + "）：不得按内置执行，也不得无边界地跑插件"
            ),
            hard_termination_required=True,
            worker_available=bool(worker_available),
            forced_available=bool(forced_available),
        )
    if not execution.plugin_governed:
        return TerminationDecision(
            mode=TerminationMode.COOPERATIVE.value,
            reason=key,
            detail="内置执行：系统默认上限，不受插件配额治理",
            worker_available=bool(worker_available),
            forced_available=bool(forced_available),
        )
    forced_required = requires_forced_termination(key)
    if not forced_required:
        return TerminationDecision(
            mode=TerminationMode.COOPERATIVE.value,
            reason=key,
            detail="协作式取消足够（超时通知/正常取消/用户停止）",
            worker_available=bool(worker_available),
            forced_available=bool(forced_available),
        )
    if not worker_available:
        return TerminationDecision(
            mode=TerminationMode.REFUSED.value,
            reason=key,
            error_code=PLUGIN_WORKER_UNAVAILABLE,
            detail="没有可用的插件 Worker（PluginManager.worker_for 未交出执行边界）",
            hard_termination_required=True,
            worker_available=False,
            forced_available=bool(forced_available),
        )
    if not forced_available:
        return TerminationDecision(
            mode=TerminationMode.REFUSED.value,
            reason=key,
            error_code=PLUGIN_QUOTA_NOT_ENFORCED,
            detail=f"该执行需要硬终止（{key}），当前只有进程内协作取消",
            hard_termination_required=True,
            worker_available=True,
            forced_available=False,
        )
    return TerminationDecision(
        mode=TerminationMode.FORCED.value,
        reason=key,
        detail="需要硬终止：必须走独立进程/容器（run_process）",
        hard_termination_required=True,
        worker_available=True,
        forced_available=True,
    )


class QuotaStatusLevel(StrEnum):
    """配额诚实状态四级（缺一不可，逐级声明）。"""

    NONE = "none"
    DECLARED = "declared"
    OBSERVED = "observed"
    WIRED = "wired"
    ENFORCED = "enforced"


def quota_status_levels(
    *,
    declared: bool = False,
    observed: bool = False,
    wired: bool = False,
    enforced: bool = False,
    cooperative_only: bool = False,
    not_enforced_because: Any = (),
) -> dict[str, Any]:
    """四级状态的**唯一**汇总口径：没真执行就绝不报 ``enforced``。

    ``cooperative_only=True``（只走过进程内协作路径）时 ``enforced`` 强制为 ``False``。
    """
    reasons = [str(item) for item in (not_enforced_because or ()) if str(item)]
    enforced_now = bool(enforced) and not cooperative_only
    if not enforced_now:
        if cooperative_only and "cooperative_only" not in reasons:
            reasons.append("cooperative_only")
        if not reasons:
            reasons.append("no_enforced_execution")
    if enforced_now:
        level = QuotaStatusLevel.ENFORCED.value
    elif wired:
        level = QuotaStatusLevel.WIRED.value
    elif observed:
        level = QuotaStatusLevel.OBSERVED.value
    elif declared:
        level = QuotaStatusLevel.DECLARED.value
    else:
        level = QuotaStatusLevel.NONE.value
    return {
        "level": level,
        "declared": bool(declared),
        "observed": bool(observed),
        "wired": bool(wired),
        "enforced": enforced_now,
        "cooperative_only": bool(cooperative_only),
        "not_enforced_because": reasons,
    }


__all__ = [
    "COOPERATIVE_TERMINATION_REASONS",
    "FORCED_TERMINATION_REASONS",
    "PLUGIN_QUOTA_NOT_ENFORCED",
    "PLUGIN_WORKER_UNAVAILABLE",
    "SYSTEM_DEFAULT_LIMITS",
    "ExecutionClass",
    "ExecutionClassification",
    "QuotaStatusLevel",
    "TerminationDecision",
    "TerminationMode",
    "TerminationReason",
    "classify_execution",
    "decide_termination",
    "forced_termination_reasons",
    "quota_status_levels",
    "requires_forced_termination",
]
