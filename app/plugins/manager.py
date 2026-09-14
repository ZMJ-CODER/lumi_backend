"""阶段 4：``PluginManager`` —— 插件生命周期的**唯一入口**（状态机 + 运行中 Job 策略）。

与 ``PluginRegistry`` 的分工：

* ``PluginRegistry`` 保留"登记 / 门禁 / 依赖 / 验签 / 健康 / 持久化"这些**事实**能力；
* ``PluginManager`` 在其上补两件控制面的事：
  1. **状态机**：install/enable/disable/upgrade/uninstall/crash 全部走
     ``lumi_contracts.plugins.lifecycle.transition()``，非法迁移直接拒绝
     （``PluginOperationError``，稳定码 ``PLUGIN_STATE_TRANSITION_ILLEGAL``）；
  2. **运行中 Job 的分级策略**：严格按 ``operation_policy()`` 的表执行——
     ``disable`` 已在跑的调用跑完、未开始的 step 复用既有预检阻断；
     ``upgrade`` 走 drain（老 Job 沿用旧版本，新 Job 用新版本，版本进 trace）；
     ``uninstall`` 在途副作用经**既有 Effect Journal** 标 ``uncertain``；
     ``crash`` 重启 + ``idempotency_key`` 去重，连续崩溃到阈值即熔断自动 ``disabled``。

灰度：``PLUGIN_QUOTA_ENFORCEMENT``（默认关闭）。**开关关闭时本类只做纯委派**——
不调用 ``transition()``、不写生命周期记录、不登记在途 step，落盘记录与返回值与
改造前逐字节一致（见 ``tests/capabilities/test_plugin_lifecycle_wiring.py`` 的等价性用例）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

# 契约模块整体持有：便于测试用 monkeypatch 钉住"开关关闭时状态机不被咨询"。
from lumi_contracts.plugins import lifecycle as plugin_lifecycle

from app.platform.runtime.feature_flags import feature_enabled
from app.plugins.registry import (
    PluginInstallation,
    PluginRegistry,
    PluginRejected,
)

#: 本阶段唯一灰度开关（与 ``Settings.PLUGIN_QUOTA_ENFORCEMENT`` 同名）。
FLAG = "PLUGIN_QUOTA_ENFORCEMENT"

#: 非法状态迁移的稳定拒绝码（应用层码，与既有 ``PLUGIN_*`` 拒绝码同风格）。
PLUGIN_STATE_TRANSITION_ILLEGAL = "PLUGIN_STATE_TRANSITION_ILLEGAL"

#: 生命周期状态在安装记录里的键（**只在开关打开时写入**）。
LIFECYCLE_RECORD_KEY = "lifecycle"


class PluginOperationError(PluginRejected):
    """生命周期操作被拒绝（继承 ``PluginRejected``：REST 层照旧映射成稳定 400）。"""


@dataclass(slots=True)
class PluginStepLease:
    """一次"运行中 step"的租约（在途登记 + 该 step 该用的插件版本）。

    ``plugin_version`` 是**该 Job 开始时**的版本：升级 drain 期间老 Job 继续用旧版本，
    新 Job 用新版本；``draining`` 为真表示这次执行跨在 drain 窗口里。
    """

    plugin_id: str
    plugin_version: str
    job_id: str = ""
    step_id: str = ""
    effect_key: str = ""
    idempotency_key: str = ""
    draining: bool = False
    #: 开关关闭时为 False：本类不维护任何在途状态（保持改造前行为）。
    tracked: bool = False

    @property
    def trace(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "plugin_draining": bool(self.draining),
        }


@dataclass(slots=True)
class DrainState:
    """一次升级的 drain 记录（``inflight`` 归零即收口）。"""

    plugin_id: str
    from_version: str
    to_version: str
    started_at: float = 0.0
    closed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "closed": bool(self.closed),
        }


@dataclass(slots=True)
class CrashOutcome:
    """一次崩溃处理的留痕（重启/去重/熔断三件事的结论）。"""

    plugin_id: str
    state: str = ""
    consecutive_crashes: int = 0
    restarted: bool = False
    deduplicated: bool = False
    circuit_broken: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "state": self.state,
            "consecutive_crashes": int(self.consecutive_crashes),
            "restarted": bool(self.restarted),
            "deduplicated": bool(self.deduplicated),
            "circuit_broken": bool(self.circuit_broken),
        }


@dataclass(slots=True)
class _Lifecycle:
    """一个插件的生命周期内存态。"""

    state: str
    consecutive_crashes: int = 0
    crash_keys: set[str] = field(default_factory=set)
    updated_at: float = 0.0
    #: 最近一次操作按 ``operation_policy()`` 得出的运行中 Job 策略（留痕/排障用）。
    last_policy: dict[str, Any] = field(default_factory=dict)


class PluginManager:
    """插件生命周期唯一入口（开关关闭时是 ``PluginRegistry`` 的纯委派）。"""

    def __init__(
        self,
        *,
        registry: PluginRegistry,
        state_store: Any = None,
        settings: Any = None,
        clock: Any = None,
    ) -> None:
        self._registry = registry
        # 复用注册表的状态存储（不建第二套存储）；没有就退化为"仅内存"。
        self._store = state_store if state_store is not None else getattr(registry, "state_store", None)
        self._settings = settings
        self._clock = clock or time.time
        self._lifecycle: dict[str, _Lifecycle] = {}
        self._inflight: dict[str, list[PluginStepLease]] = {}
        self._drains: dict[str, DrainState] = {}
        self._job_versions: dict[str, str] = {}
        self._load()

    # ── 灰度 ──────────────────────────────────────────────

    def enabled(self) -> bool:
        """开关状态（每次读取 settings，便于 monkeypatch 与热更新）。"""
        return bool(feature_enabled(FLAG, settings=self._settings))

    # ── 读（两个开关态都走既有注册表事实） ─────────────────

    @property
    def registry(self) -> PluginRegistry:
        return self._registry

    def all(self) -> list[PluginInstallation]:
        return self._registry.all()

    def enabled_plugins(self) -> list[PluginInstallation]:
        return self._registry.enabled()

    def get(self, plugin_id: str) -> PluginInstallation | None:
        return self._registry.get(plugin_id)

    def require(self, plugin_id: str) -> PluginInstallation:
        return self._registry.require(plugin_id)

    def installed_views(self) -> list[Any]:
        return self._registry.installed_views()

    def plugin_snapshot(self) -> dict[str, Any]:
        return self._registry.plugin_snapshot()

    def health(self, plugin_id: str) -> dict[str, Any]:
        return self._registry.health(plugin_id)

    def health_check(self, manifest: Any) -> tuple[str, str]:
        return self._registry.health_check(manifest)

    def lifecycle_state(self, plugin_id: str) -> str:
        """当前生命周期状态。

        开关关闭：**不咨询状态机**，只按既有 ``enabled`` 事实给出等价字符串
        （新插件在注册表里是"已启用/未启用"，没有 ``registered`` 这个概念）。
        """
        if not self.enabled():
            installation = self._registry.get(plugin_id)
            if installation is None:
                return ""
            return "enabled" if installation.enabled else "disabled"
        return self._current_state(plugin_id)

    def consecutive_crashes(self, plugin_id: str) -> int:
        record = self._lifecycle.get(str(plugin_id))
        return int(record.consecutive_crashes) if record else 0

    def drain_state(self, plugin_id: str) -> DrainState | None:
        return self._drains.get(str(plugin_id))

    def active_version(self, plugin_id: str) -> str:
        installation = self._registry.get(plugin_id)
        return installation.version if installation is not None else ""

    def trace_fields(self, *, plugin_id: str = "", job_id: str = "") -> dict[str, Any]:
        """该 Job / 插件在 trace 里可见的字段（``plugin_version`` 是 drain 的可见证据）。"""
        installation = self._registry.get(plugin_id) if plugin_id else None
        version = self.active_version(plugin_id) if plugin_id else ""
        drain = self._drains.get(str(plugin_id)) if plugin_id else None
        if job_id and job_id in self._job_versions:
            version = self._job_versions[job_id]
        return {
            "plugin_id": str(plugin_id or ""),
            "plugin_version": version,
            "plugin_state": self.lifecycle_state(plugin_id) if plugin_id else "",
            "plugin_draining": bool(drain and not drain.closed),
            "plugin_installed": installation is not None,
        }

    def blocked_capabilities(self) -> set[str]:
        """被停用/卸载（或注册未启用）插件提供的能力名（**基础名**，不带 ``@版本``）。

        这是"disable → 未开始的 step 走预检阻断"的**唯一事实来源**：预检链路
        （``capability_broker.preflight_facts`` → ``CapabilityPreflightService``）把它
        翻成既有错误码 ``CAPABILITY_UNAVAILABLE``，本模块不新增阻断机制。
        """
        if not self.enabled():
            return set()
        blocked: set[str] = set()
        for installation in self._registry.all():
            state = self._current_state(installation.plugin_id)
            if state not in {
                plugin_lifecycle.PluginState.DISABLED.value,
                plugin_lifecycle.PluginState.UNINSTALLING.value,
                plugin_lifecycle.PluginState.UNINSTALLED.value,
            } and installation.enabled:
                continue
            for name in installation.manifest.provides.capabilities:
                base = str(name or "").split("@", 1)[0].strip()
                if base:
                    blocked.add(base)
        return blocked

    # ── 生命周期（开关关闭 = 纯委派） ──────────────────────

    def worker_for(
        self,
        plugin_id: str,
        *,
        artifact_sink: Any = None,
        owner: str = "",
        container: str = "",
    ) -> Any:
        """该插件**当前版本**的配额 Worker（Manifest 声明 → Worker 真执行边界）。

        这是"声明 → 执行"的唯一接线点：``resource_limits`` 由这里变成
        ``PluginQuotaSpec``，执行侧（沙箱 / Worker）据此真杀进程、真限流、真转产物引用。
        开关关闭时返回的 Worker 是纯透传（不判定、不杀、不落产物）。
        """
        from app.plugins.quota import PluginWorker, quota_spec_for

        installation = self._registry.require(plugin_id)
        spec = quota_spec_for(installation, plugin_version=installation.version)
        return PluginWorker(
            spec=spec,
            artifact_sink=artifact_sink,
            owner=owner,
            container=container,
            settings=self._settings,
        )

    def quota_status(self, plugin_id: str) -> dict[str, Any]:
        """该插件的配额**诚实**状态（declared / observed / wired / enforced 四级）。

        唯一口径在 ``app.plugins.boundary``：只有"开关打开 + 硬约束 +
        真走过可强杀进程"才算 ``enforced``，协作式**不算**。
        """
        from app.plugins.boundary import plugin_quota_status

        return plugin_quota_status(self, plugin_id, settings=self._settings)

    def quota_statuses(self) -> list[dict[str, Any]]:
        """全部已安装插件的配额状态（列表接口用）。"""
        from app.plugins.boundary import plugin_quota_statuses

        return plugin_quota_statuses(self, settings=self._settings)

    async def install(
        self,
        manifest: Any,
        *,
        files: dict[str, bytes] | None = None,
        signature_policy: Any = None,
        activate: bool = True,
    ) -> PluginInstallation:
        if not self.enabled():
            return self._registry.install(
                manifest, files=files, signature_policy=signature_policy, activate=activate
            )
        plugin_id = manifest.id
        target = self._install_target(plugin_id, activate=activate)
        installation = self._registry.install(
            manifest, files=files, signature_policy=signature_policy, activate=activate
        )
        self._commit(plugin_id, target, reset_crashes=True)
        return installation

    async def enable(self, plugin_id: str) -> PluginInstallation:
        if not self.enabled():
            return self._registry.enable(plugin_id)
        target = self._validate(plugin_id, plugin_lifecycle.PluginState.ENABLED.value)
        installation = self._registry.enable(plugin_id)
        self._commit(plugin_id, target, reset_crashes=True)
        return installation

    async def disable(self, plugin_id: str, *, reason: str = "") -> PluginInstallation:
        """停用：已开始的调用**不回收**（在途租约保持可完成），未开始的 step 走既有预检。"""
        if not self.enabled():
            return self._registry.disable(plugin_id, reason=reason)
        target = self._validate(plugin_id, plugin_lifecycle.PluginState.DISABLED.value)
        installation = self._registry.disable(plugin_id, reason=reason)
        self._commit(plugin_id, target, policy=plugin_lifecycle.operation_policy("disable"))
        # inflight="finish"：这里**刻意不动**在途租约，老 Job 继续跑完当前 step。
        return installation

    async def upgrade(
        self,
        manifest: Any,
        *,
        files: dict[str, bytes] | None = None,
        signature_policy: Any = None,
    ) -> PluginInstallation:
        """升级：开 drain。老 Job 沿用旧版本，新 Job 用新版本；无在途则立即收口。"""
        if not self.enabled():
            return self._registry.upgrade(manifest, files=files, signature_policy=signature_policy)
        plugin_id = manifest.id
        existing = self._registry.require(plugin_id)
        from_version = existing.version
        policy = plugin_lifecycle.operation_policy("upgrade")
        target = self._validate(plugin_id, plugin_lifecycle.PluginState.UPGRADING.value)
        installation = self._registry.upgrade(
            manifest, files=files, signature_policy=signature_policy
        )
        self._commit(plugin_id, target, policy=policy)
        if policy.requires_drain:
            self._drains[plugin_id] = DrainState(
                plugin_id=plugin_id,
                from_version=from_version,
                to_version=installation.version,
                started_at=float(self._clock()),
            )
            self._close_drain_if_idle(plugin_id)
        return installation

    async def uninstall(self, plugin_id: str) -> bool:
        """卸载：在途副作用经**既有 Effect Journal** 标 ``uncertain``，再摘除登记。"""
        if not self.enabled():
            return self._registry.uninstall(plugin_id)
        policy = plugin_lifecycle.operation_policy("uninstall")
        target = self._validate(plugin_id, plugin_lifecycle.PluginState.UNINSTALLING.value)
        self._commit(plugin_id, target, policy=policy)
        if policy.marks_uncertain:
            await self._mark_inflight_uncertain(plugin_id)
        removed = self._registry.uninstall(plugin_id)
        self._commit(plugin_id, plugin_lifecycle.PluginState.UNINSTALLED.value, policy=policy)
        self._forget_runtime(plugin_id)
        return removed

    def rollback(self, plugin_id: str, *, version: str = "") -> PluginInstallation:
        """回滚：注册表事实 + 状态同步（回滚后是"未启用"，需重新启用过门禁）。"""
        installation = self._registry.rollback(plugin_id, version=version)
        if self.enabled():
            self._commit(
                plugin_id,
                plugin_lifecycle.PluginState.ENABLED.value
                if installation.enabled
                else plugin_lifecycle.PluginState.DISABLED.value,
            )
        return installation

    # ── 崩溃 / 熔断 ───────────────────────────────────────

    def record_crash(
        self,
        plugin_id: str,
        *,
        idempotency_key: str = "",
        error: str = "",
    ) -> CrashOutcome | None:
        """崩溃 → 重启 + 幂等键去重；连续崩溃到阈值 → 熔断（自动 ``disabled`` + 告警）。

        开关关闭时返回 ``None``，**不记录、不重启、不停用**（保持改造前行为）。
        """
        if not self.enabled():
            return None
        plugin_id = str(plugin_id)
        record = self._lifecycle.get(plugin_id)
        if record is None:
            current = self._current_state(plugin_id)
        else:
            current = record.state
        if idempotency_key and record is not None and idempotency_key in record.crash_keys:
            return CrashOutcome(
                plugin_id=plugin_id,
                state=current,
                consecutive_crashes=int(record.consecutive_crashes),
                deduplicated=True,
            )
        if record is None:
            record = _Lifecycle(state=current, updated_at=float(self._clock()))
            self._lifecycle[plugin_id] = record
        if idempotency_key:
            record.crash_keys.add(str(idempotency_key))
        # 策略表是唯一事实源：``crash`` 的 inflight="restart"、new_steps="normal"
        record.last_policy = dict(plugin_lifecycle.operation_policy("crash").as_dict())

        crashed = self._validate(plugin_id, plugin_lifecycle.PluginState.CRASHED.value)
        record.state = crashed
        record.consecutive_crashes = int(record.consecutive_crashes) + 1
        record.updated_at = float(self._clock())

        # 重启：崩溃态 → 启用态（同一次崩溃只算一次，靠上面的幂等键去重）。
        restarted_state = self._validate(plugin_id, plugin_lifecycle.PluginState.ENABLED.value)
        record.state = restarted_state
        outcome = CrashOutcome(
            plugin_id=plugin_id,
            state=restarted_state,
            consecutive_crashes=int(record.consecutive_crashes),
            restarted=True,
        )
        if plugin_lifecycle.should_circuit_break(record.consecutive_crashes):
            record.state = self._validate(plugin_id, plugin_lifecycle.PluginState.DISABLED.value)
            outcome.state = record.state
            outcome.circuit_broken = True
            try:
                self._registry.disable(plugin_id, reason="crash_circuit_breaker")
            except PluginRejected as exc:  # 依赖方仍在启用：熔断失败也不能把状态说成停用
                logger.warning("[plugin] {} 熔断停用失败：{}", plugin_id, str(exc)[:160])
                outcome.circuit_broken = False
                record.state = restarted_state
                outcome.state = restarted_state
            else:
                logger.warning(
                    "[plugin] {} 连续崩溃 {} 次（阈值 {}），已熔断并自动停用：{}",
                    plugin_id,
                    record.consecutive_crashes,
                    plugin_lifecycle.CRASH_CIRCUIT_THRESHOLD,
                    str(error or "")[:160],
                )
        self._persist_lifecycle(plugin_id)
        return outcome

    def note_success(self, plugin_id: str) -> None:
        """一次成功执行：清零连续崩溃计数（开关关闭时是空操作）。"""
        if not self.enabled():
            return
        record = self._lifecycle.get(str(plugin_id))
        if record is not None and record.consecutive_crashes:
            record.consecutive_crashes = 0
            record.updated_at = float(self._clock())
            self._persist_lifecycle(str(plugin_id))

    # ── 在途 step（drain / uninstall 的事实来源） ──────────

    def begin_step(
        self,
        plugin_id: str,
        *,
        job_id: str = "",
        step_id: str = "",
        effect_key: str = "",
        idempotency_key: str = "",
    ) -> PluginStepLease:
        """登记一次即将执行的 step，返回该 step 该用的插件版本。

        * 开关关闭：只读注册表版本，**不做任何在途登记**（保持改造前行为）；
        * 开关打开：新 step 一律用**新版本**；升级前已开始的 Job 沿用其旧版本（drain）。
        """
        plugin_id = str(plugin_id)
        installation = self._registry.get(plugin_id)
        version = installation.version if installation is not None else ""
        if not self.enabled():
            return PluginStepLease(plugin_id=plugin_id, plugin_version=version)
        if job_id:
            version = self._job_versions.setdefault(job_id, version)
        drain = self._drains.get(plugin_id)
        lease = PluginStepLease(
            plugin_id=plugin_id,
            plugin_version=version,
            job_id=str(job_id),
            step_id=str(step_id),
            effect_key=str(effect_key),
            idempotency_key=str(idempotency_key),
            draining=bool(drain and not drain.closed),
            tracked=True,
        )
        self._inflight.setdefault(plugin_id, []).append(lease)
        return lease

    def finish_step(self, lease: PluginStepLease | None) -> None:
        """一次 step 结束（成功或失败）：在途计数减一，drain 归零即收口。"""
        if lease is None or not lease.tracked or not self.enabled():
            return
        rows = self._inflight.get(lease.plugin_id)
        if rows:
            self._inflight[lease.plugin_id] = [item for item in rows if item is not lease]
        self._close_drain_if_idle(lease.plugin_id)

    def inflight_count(self, plugin_id: str) -> int:
        return len(self._inflight.get(str(plugin_id)) or [])

    # ── 内部 ──────────────────────────────────────────────

    def _current_state(self, plugin_id: str) -> str:
        installation = self._registry.get(plugin_id)
        if installation is None:
            record = self._lifecycle.get(str(plugin_id))
            return record.state if record is not None else plugin_lifecycle.PluginState.REGISTERED.value
        record = self._lifecycle.get(str(plugin_id))
        if record is not None and record.state:
            return record.state
        return (
            plugin_lifecycle.PluginState.ENABLED.value
            if installation.enabled
            else plugin_lifecycle.PluginState.DISABLED.value
        )

    def _validate(self, plugin_id: str, target: str) -> str:
        """只校验迁移合法性（不改动任何状态）；非法迁移转成稳定拒绝码。"""
        current = self._current_state(plugin_id)
        try:
            return plugin_lifecycle.transition(current, target)
        except ValueError as exc:
            raise PluginOperationError(
                PLUGIN_STATE_TRANSITION_ILLEGAL,
                f"插件 {plugin_id} 的生命周期迁移被拒绝：{exc}",
                details={"plugin_id": plugin_id, "from": current, "to": str(target)},
            ) from exc

    def _install_target(self, plugin_id: str, *, activate: bool) -> str:
        current = self._current_state(plugin_id)
        if current == plugin_lifecycle.PluginState.UNINSTALLED.value:
            # 已卸载 = 登记不存在：重新安装是**新登记**（terminal 态本身仍不可迁移）。
            current = plugin_lifecycle.PluginState.REGISTERED.value
        if current == plugin_lifecycle.PluginState.REGISTERED.value:
            target = (
                plugin_lifecycle.PluginState.ENABLED.value
                if activate
                else plugin_lifecycle.PluginState.REGISTERED.value
            )
        else:
            target = (
                plugin_lifecycle.PluginState.ENABLED.value
                if activate
                else plugin_lifecycle.PluginState.DISABLED.value
            )
        try:
            return plugin_lifecycle.transition(current, target)
        except ValueError as exc:
            raise PluginOperationError(
                PLUGIN_STATE_TRANSITION_ILLEGAL,
                f"插件 {plugin_id} 的安装迁移被拒绝：{exc}",
                details={"plugin_id": plugin_id, "from": current, "to": target},
            ) from exc

    def _commit(
        self,
        plugin_id: str,
        state: str,
        *,
        reset_crashes: bool = False,
        policy: Any = None,
    ) -> None:
        key = str(plugin_id)
        record = self._lifecycle.get(key)
        if record is None:
            record = _Lifecycle(state=str(state), updated_at=float(self._clock()))
            self._lifecycle[key] = record
        record.state = str(state)
        record.updated_at = float(self._clock())
        if policy is not None:
            record.last_policy = dict(policy.as_dict())
        if reset_crashes:
            record.consecutive_crashes = 0
            record.crash_keys.clear()
        self._persist_lifecycle(key)

    def _persist_lifecycle(self, plugin_id: str) -> None:
        """把生命周期状态合并进**既有**安装记录（开关关闭时本方法不会被调用）。"""
        store = self._store
        record = self._lifecycle.get(str(plugin_id))
        if store is None or record is None:
            return
        try:
            row = store.get(plugin_id)
            if row is None:
                return
            row[LIFECYCLE_RECORD_KEY] = {
                "state": record.state,
                "consecutive_crashes": int(record.consecutive_crashes),
                "updated_at": float(record.updated_at),
                "operation_policy": dict(record.last_policy),
            }
            store.put(row)
        except Exception as exc:  # noqa: BLE001 - 生命周期留痕失败不该让生命周期操作失败
            logger.warning("[plugin] 生命周期状态落盘失败（仅内存生效）: {}", str(exc)[:160])

    def _load(self) -> None:
        """恢复生命周期状态（只有开关打开过的记录里才有这个键）。"""
        store = self._store
        if store is None:
            return
        try:
            rows = {str(row.get("plugin_id")): row for row in store.all()}
        except Exception as exc:  # noqa: BLE001
            logger.warning("[plugin] 读取生命周期状态失败（按空处理）: {}", str(exc)[:160])
            return
        for plugin_id, row in rows.items():
            payload = row.get(LIFECYCLE_RECORD_KEY)
            if not isinstance(payload, dict) or not payload.get("state"):
                continue
            self._lifecycle[plugin_id] = _Lifecycle(
                state=str(payload["state"]),
                consecutive_crashes=int(payload.get("consecutive_crashes") or 0),
                updated_at=float(payload.get("updated_at") or 0.0),
            )

    def _close_drain_if_idle(self, plugin_id: str) -> None:
        drain = self._drains.get(str(plugin_id))
        if drain is None or drain.closed:
            return
        if self.inflight_count(plugin_id):
            return
        drain.closed = True
        record = self._lifecycle.get(str(plugin_id))
        if record is not None:
            record.state = plugin_lifecycle.transition(
                record.state, plugin_lifecycle.PluginState.ENABLED.value
            )
            record.updated_at = float(self._clock())
        self._persist_lifecycle(str(plugin_id))

    async def _mark_inflight_uncertain(self, plugin_id: str) -> list[str]:
        """在途副作用 → ``uncertain``（复用既有 Effect Journal，不建第二套）。"""
        rows = list(self._inflight.get(str(plugin_id)) or [])
        marked: list[str] = []
        if not rows:
            return marked
        from app.agents.orchestration.runtime import effects

        for lease in rows:
            if not lease.effect_key:
                continue
            try:
                await effects.mark_effect_uncertain(lease.effect_key, "plugin_uninstalled")
            except Exception as exc:  # noqa: BLE001 - 日志不可用不能让卸载失败
                logger.warning(
                    "[plugin] {} 在途副作用标记 uncertain 失败（key={}）: {}",
                    plugin_id, lease.effect_key[:48], str(exc)[:160],
                )
                continue
            marked.append(lease.effect_key)
        return marked

    def _forget_runtime(self, plugin_id: str) -> None:
        plugin_id = str(plugin_id)
        self._inflight.pop(plugin_id, None)
        self._drains.pop(plugin_id, None)


# ── 进程内唯一入口 ────────────────────────────────────────

_manager: PluginManager | None = None


def set_plugin_manager(manager: PluginManager | None) -> None:
    """登记进程内唯一管理器（REST 层导入时调用；测试可显式覆盖）。"""
    global _manager
    _manager = manager


def active_plugin_manager() -> PluginManager | None:
    """取进程内管理器；**开关关闭时直接返回 ``None``**（不建注册表、不读状态文件）。"""
    global _manager
    if _manager is not None:
        return _manager
    if not feature_enabled(FLAG):
        return None
    from app.plugins.registry import PluginRegistry as _Registry
    from app.plugins.state import PluginStateStore as _Store

    _manager = PluginManager(registry=_Registry(store=_Store()))
    return _manager


def plugin_blocked_capabilities() -> set[str]:
    """被停用/卸载插件提供的能力（预检探测的唯一插件侧事实来源）。

    开关关闭 → 空集合，且**不会构造管理器**（没有额外 I/O、没有状态机咨询）。
    """
    manager = active_plugin_manager()
    if manager is None:
        return set()
    return manager.blocked_capabilities()


__all__ = [
    "FLAG",
    "LIFECYCLE_RECORD_KEY",
    "PLUGIN_STATE_TRANSITION_ILLEGAL",
    "CrashOutcome",
    "DrainState",
    "PluginManager",
    "PluginOperationError",
    "PluginStepLease",
    "active_plugin_manager",
    "plugin_blocked_capabilities",
    "set_plugin_manager",
]
