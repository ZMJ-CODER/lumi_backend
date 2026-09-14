"""阶段 4 收口：插件**执行边界**在应用侧的接线（``worker_for`` 是唯一入口）。

契约层（``lumi_contracts.plugins.execution``）只做纯判定；本模块负责"把它接到真实执行上"：

* :func:`resolve_execution` —— **唯一**的取边界动作：插件执行必须经
  ``PluginManager.worker_for(plugin_id)`` 拿到带 Manifest 配额的 Worker；
  拿不到就按契约阻断（``PLUGIN_WORKER_UNAVAILABLE`` / ``PLUGIN_QUOTA_NOT_ENFORCED``），
  **绝不**回退到进程内执行再报成功；
* :func:`plugin_quota_status` —— 诚实状态四级 ``declared`` / ``observed`` / ``wired`` /
  ``enforced``（只走过协作式就不是 ``enforced``）；
* 执行证据（``wired`` / ``observed`` / ``forced`` / ``cooperative``）由**真实执行边界**
  （``PluginWorker.run_process`` / ``PluginWorker.run``）写入 :func:`record_execution`，
  不是调用方自报。

灰度：``PLUGIN_QUOTA_ENFORCEMENT`` 关闭时 :func:`resolve_execution` 直接返回
``passthrough=True`` 的计划——不取 Worker、不记证据、不阻断，调用方按既有路径执行，
与改造前逐字节一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from lumi_contracts.events.errors import spec_for
from lumi_contracts.plugins.execution import (
    PLUGIN_QUOTA_NOT_ENFORCED,
    PLUGIN_WORKER_UNAVAILABLE,
    ExecutionClassification,
    QuotaStatusLevel,
    TerminationDecision,
    TerminationMode,
    TerminationReason,
    classify_execution,
    decide_termination,
    quota_status_levels,
)

from app.platform.runtime.feature_flags import feature_enabled
from app.plugins.manager import FLAG
from app.plugins.registry import PluginRejected

__all__ = [
    "PLUGIN_QUOTA_NOT_ENFORCED",
    "PLUGIN_WORKER_UNAVAILABLE",
    "ExecutionPlan",
    "PluginQuotaBlocked",
    "QuotaEvidence",
    "blocked_error",
    "evidence_for",
    "plugin_quota_status",
    "plugin_quota_statuses",
    "record_execution",
    "record_refused",
    "record_wired",
    "reset_quota_evidence",
    "resolve_execution",
]


def _enabled(settings: Any = None) -> bool:
    return bool(feature_enabled(FLAG, settings=settings))


# ── 执行证据（进程内计数；只由真实执行边界写入） ─────────────


@dataclass(slots=True)
class QuotaEvidence:
    """一个插件的执行证据（``wired``/``observed``/``forced``/``cooperative``/``refused``）。"""

    wired: int = 0
    observed: int = 0
    forced: int = 0
    cooperative: int = 0
    refused: int = 0
    last: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "wired": int(self.wired),
            "observed": int(self.observed),
            "forced": int(self.forced),
            "cooperative": int(self.cooperative),
            "refused": int(self.refused),
            "cooperative_only": bool(self.wired and not self.forced),
            "last": dict(self.last),
        }


_EVIDENCE: dict[str, QuotaEvidence] = {}


def evidence_for(plugin_id: Any) -> QuotaEvidence:
    """读取（必要时创建）该插件的证据计数。"""
    key = str(plugin_id or "")
    record = _EVIDENCE.get(key)
    if record is None:
        record = QuotaEvidence()
        _EVIDENCE[key] = record
    return record


def reset_quota_evidence(plugin_id: Any = "") -> None:
    """清空证据（测试/长时间运行后的重置）。``plugin_id`` 为空清空全部。"""
    key = str(plugin_id or "")
    if not key:
        _EVIDENCE.clear()
        return
    _EVIDENCE.pop(key, None)


def record_wired(plugin_id: Any, *, mode: str = "", quota_spec: Any = None, detail: Any = None) -> None:
    """记录"这次执行真的经 ``worker_for`` 拿到了 Worker"（``wired`` 的唯一来源）。"""
    key = str(plugin_id or "")
    if not key:
        return
    record = evidence_for(key)
    record.wired += 1
    record.last = {
        "event": "wired",
        "mode": str(mode or ""),
        "hard_limits": bool(getattr(quota_spec, "hard_limits", False)),
        "plugin_version": str(getattr(quota_spec, "plugin_version", "") or ""),
        **dict(detail or {}),
    }


def record_execution(
    plugin_id: Any,
    *,
    mode: str = TerminationMode.COOPERATIVE.value,
    output_bytes: int = 0,
    action: str = "",
    killed: bool = False,
    error_code: str = "",
    elapsed_seconds: float = 0.0,
) -> None:
    """记录一次**真实**执行上报的用量（``observed`` 的唯一来源）。

    ``mode == forced`` 才算"硬约束真的被用上"；协作式只增加 ``cooperative``。
    """
    key = str(plugin_id or "")
    if not key:
        return
    record = evidence_for(key)
    record.observed += 1
    if str(mode) == TerminationMode.FORCED.value:
        record.forced += 1
    else:
        record.cooperative += 1
    record.last = {
        "event": "executed",
        "mode": str(mode or ""),
        "action": str(action or ""),
        "killed": bool(killed),
        "output_bytes": int(output_bytes or 0),
        "elapsed_seconds": round(float(elapsed_seconds or 0.0), 6),
        "error_code": str(error_code or ""),
    }


def record_refused(plugin_id: Any, *, error_code: str = "", detail: str = "") -> None:
    """记录一次被边界拒绝的执行（排障用；拒绝也算"边界生效"的证据）。"""
    key = str(plugin_id or "")
    if not key:
        return
    record = evidence_for(key)
    record.refused += 1
    record.last = {
        "event": "refused",
        "error_code": str(error_code or ""),
        "detail": str(detail or "")[:200],
    }


# ── 计划 / 阻断 ──────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """一次执行的边界结论（调用方据此决定怎么跑，或直接阻断）。"""

    classification: ExecutionClassification
    termination: TerminationDecision
    worker: Any = None
    quota_spec: Any = None
    passthrough: bool = False
    detail: str = ""

    @property
    def plugin_id(self) -> str:
        return self.classification.plugin_id

    @property
    def plugin_version(self) -> str:
        return str(
            getattr(self.quota_spec, "plugin_version", "")
            or self.classification.plugin_version
            or ""
        )

    @property
    def refused(self) -> bool:
        return bool(self.termination.refused)

    @property
    def error_code(self) -> str:
        return str(self.termination.error_code or "")

    @property
    def mode(self) -> str:
        return str(self.termination.mode)

    @property
    def forced(self) -> bool:
        return self.mode == TerminationMode.FORCED.value

    @property
    def cooperative(self) -> bool:
        return self.mode == TerminationMode.COOPERATIVE.value

    @property
    def plugin_governed(self) -> bool:
        return bool(self.classification.plugin_governed and not self.passthrough)

    @property
    def safe_message(self) -> str:
        """阻断码的登记文案（前端只展示它，不放内部细节）。"""
        if not self.error_code:
            return ""
        return str(spec_for(self.error_code).safe_message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "passthrough": bool(self.passthrough),
            "refused": self.refused,
            "error_code": self.error_code,
            "safe_message": self.safe_message,
            "mode": self.mode,
            "classification": self.classification.as_dict(),
            "termination": self.termination.as_dict(),
            "quota": (
                None
                if self.quota_spec is None
                else {
                    "hard_limits": bool(getattr(self.quota_spec, "hard_limits", False)),
                    "cpu_seconds": float(getattr(self.quota_spec, "cpu_seconds", 0.0) or 0.0),
                    "max_concurrency": int(getattr(self.quota_spec, "max_concurrency", 1) or 1),
                }
            ),
            "detail": self.detail,
        }


class PluginQuotaBlocked(PluginRejected):
    """插件执行被边界阻断（稳定错误码；REST 层照旧映射 400）。"""


def blocked_error(plan: ExecutionPlan) -> PluginQuotaBlocked:
    """把被拒绝的计划翻成带稳定码的异常（非 ToolOutput 调用方用）。"""
    code = plan.error_code or PLUGIN_QUOTA_NOT_ENFORCED
    message = plan.safe_message or str(plan.detail or "插件执行被拒绝")
    return PluginQuotaBlocked(code, message, details=plan.as_dict())


# ── 唯一取边界动作 ────────────────────────────────────────


async def resolve_execution(
    *,
    manager: Any = None,
    plugin_id: Any = "",
    manifest: Any = None,
    quota_spec: Any = None,
    termination: Any = "",
    forced_available: bool = True,
    owner: str = "",
    container: str = "",
    artifact_sink: Any = None,
    settings: Any = None,
) -> ExecutionPlan:
    """把一次执行接到 Worker 边界上；返回被拒绝的计划时调用方**必须**失败。

    调用链（插件执行）：

        PluginManager → worker_for(plugin_id) → Worker 配额执行 → run_script/python_exec

    * 开关关闭 → ``passthrough=True``（不取 Worker、不记证据、不阻断）；
    * ``plugin_id`` 为空 → 显式 ``builtin``（系统默认上限，不按插件配额治理）；
    * ``plugin_id`` 非空 → 必须拿到 Worker：拿不到 → ``PLUGIN_WORKER_UNAVAILABLE``；
      需要硬终止但只有协作式 → ``PLUGIN_QUOTA_NOT_ENFORCED``；
      带了配额声明却没有插件归属 → ``PLUGIN_QUOTA_NOT_ENFORCED``。
    """
    key = str(plugin_id or "").strip()
    if not _enabled(settings):
        classification = classify_execution(
            plugin_id=key, manifest=manifest, quota_spec=quota_spec
        )
        return ExecutionPlan(
            classification=classification,
            termination=TerminationDecision(
                mode=TerminationMode.COOPERATIVE.value,
                reason=str(termination or TerminationReason.TIMEOUT_NOTIFY.value),
                detail="开关关闭：与改造前一致（不取 Worker、不判定、不阻断）",
            ),
            passthrough=True,
            detail="flag_off",
        )

    worker: Any = None
    refusal = ""
    if key:
        installation = manager.get(key) if manager is not None else None
        if installation is None:
            refusal = f"插件 {key} 没有可用的 Worker（未安装或没有插件管理器）"
        else:
            if manifest is None:
                manifest = installation.manifest
            try:
                worker = manager.worker_for(
                    key, artifact_sink=artifact_sink, owner=owner, container=container
                )
            except Exception as exc:  # noqa: BLE001 - 取边界失败必须阻断，不是静默回退
                logger.warning("[plugin] {} 取 Worker 失败：{}", key, str(exc)[:160])
                worker = None
                refusal = f"插件 {key} 的 Worker 不可用：{type(exc).__name__}"
            if worker is None and not refusal:
                refusal = f"插件 {key} 没有可用的 Worker（worker_for 未交出执行边界）"
            elif worker is not None and quota_spec is None:
                quota_spec = getattr(worker, "spec", None)
                if quota_spec is None:
                    refusal = f"插件 {key} 的 Worker 未提供配额视图"

    classification = classify_execution(
        plugin_id=key, manifest=manifest, quota_spec=quota_spec
    )

    if refusal:
        # **带插件身份却没有 Worker** → 一律 PLUGIN_WORKER_UNAVAILABLE（主阻断码）：
        # 即使 Manifest/配额视图还没拿到，也不能回退成内置执行再报成功。
        decision = TerminationDecision(
            mode=TerminationMode.REFUSED.value,
            reason=str(termination or TerminationReason.RUNAWAY_LOOP.value),
            error_code=PLUGIN_WORKER_UNAVAILABLE,
            detail=refusal,
            hard_termination_required=True,
            worker_available=False,
            forced_available=bool(forced_available),
        )
        record_refused(key, error_code=decision.error_code, detail=refusal)
        logger.warning("[plugin] {} 执行被阻断（{}）：{}", key, decision.error_code, refusal)
        return ExecutionPlan(
            classification=classification,
            termination=decision,
            quota_spec=quota_spec,
            detail=refusal,
        )

    decision = decide_termination(
        execution=classification,
        reason=termination,
        worker_available=worker is not None,
        forced_available=bool(forced_available),
    )
    if decision.refused:
        record_refused(key, error_code=decision.error_code, detail=decision.detail)
        logger.warning(
            "[plugin] {} 执行被阻断（{}）：{}",
            key or "-",
            decision.error_code,
            decision.detail,
        )
        return ExecutionPlan(
            classification=classification,
            termination=decision,
            worker=worker,
            quota_spec=quota_spec,
            detail=decision.detail,
        )
    if classification.plugin_governed:
        record_wired(key, mode=decision.mode, quota_spec=quota_spec)
    return ExecutionPlan(
        classification=classification,
        termination=decision,
        worker=worker,
        quota_spec=quota_spec if classification.plugin_governed else None,
        detail=decision.detail,
    )


# ── 诚实状态四级 ──────────────────────────────────────────

#: 四级的含义（API 自描述；前端直接展示，不必自己解释）。
LEVEL_MEANING: dict[str, str] = {
    QuotaStatusLevel.DECLARED.value: "Manifest 声明了 resource_limits（只有声明）",
    QuotaStatusLevel.OBSERVED.value: "已有真实执行上报用量（只是观察，未约束）",
    QuotaStatusLevel.WIRED.value: "执行真的经 PluginManager.worker_for → Worker（接线完成）",
    QuotaStatusLevel.ENFORCED.value: "开关打开 + 硬约束 + 真走可强杀进程（协作式不算）",
}


def plugin_quota_status(manager: Any, plugin_id: Any, *, settings: Any = None) -> dict[str, Any]:
    """一个插件的配额**诚实**状态：declared / observed / wired / enforced。"""
    from app.plugins.quota import quota_spec_for

    key = str(plugin_id or "")
    installation = manager.get(key) if manager is not None else None
    declared_spec = (
        quota_spec_for(installation, plugin_version=installation.version)
        if installation is not None
        else None
    )
    flag_on = _enabled(settings)
    evidence = evidence_for(key)
    hard_limits = bool(getattr(declared_spec, "hard_limits", False))
    cooperative_only = bool(evidence.wired and not evidence.forced)
    enforced = bool(
        flag_on and hard_limits and installation is not None and evidence.forced > 0
        and not cooperative_only
    )
    reasons: list[str] = []
    if not flag_on:
        reasons.append("flag_off")
    if installation is None:
        reasons.append("plugin_not_installed")
    elif not hard_limits:
        reasons.append("builtin_soft_limits")
    if evidence.wired == 0:
        reasons.append("no_wired_execution")
    if cooperative_only:
        reasons.append("cooperative_only")
    levels = quota_status_levels(
        declared=installation is not None,
        observed=evidence.observed > 0,
        wired=evidence.wired > 0,
        enforced=enforced,
        cooperative_only=cooperative_only,
        not_enforced_because=reasons,
    )
    return {
        "plugin_id": key,
        "plugin_version": installation.version if installation is not None else "",
        "level": levels["level"],
        "levels": levels,
        "flag": FLAG,
        "flag_enabled": flag_on,
        "limits": declared_spec.as_dict() if declared_spec is not None else None,
        "enforcement": {
            "flag_enabled": flag_on,
            "hard_limits": hard_limits,
            # PluginWorker.run_process 总能真起子进程并 kill（唯一的强杀路径）。
            "forced_path_available": bool(flag_on and hard_limits),
            "cooperative_only": cooperative_only,
            "enforced": bool(levels["enforced"]),
            "not_enforced_because": list(levels["not_enforced_because"]),
        },
        "evidence": evidence.as_dict(),
        "level_meaning": dict(LEVEL_MEANING),
    }


def plugin_quota_statuses(manager: Any, *, settings: Any = None) -> list[dict[str, Any]]:
    """全部已安装插件的配额状态（列表接口用）。"""
    if manager is None:
        return []
    return [
        plugin_quota_status(manager, installation.plugin_id, settings=settings)
        for installation in manager.all()
    ]
