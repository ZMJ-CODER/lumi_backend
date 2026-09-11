"""阶段 4：PluginRegistry —— 插件生命周期（安装/启用/停用/升级/回滚/健康）。

安装流程（方案第二节 2）：

    校验 Manifest → 校验签名 → 检查依赖 → 检查部署位置 → 检查权限与风险
    → 登记安装记录 → 健康检查 → 原子激活（启用）→ 写插件快照

四类插件的位置约束（"服务端不能对第三方插件用任意 importlib 进 API 进程"）：

* ``builtin``：进程内（本阶段的内置能力 Provider 走这条路）；
* ``official``：受限 Worker（``isolation=restricted_worker``）；
* ``third_party``：独立 Worker/容器（``isolation=sandboxed``）；
* ``local_dev``：仅开发者模式，且**不**允许任意远程路径加载。

本阶段不真正加载第三方代码：Registry 只做"登记 + 门禁 + 健康检查 + 状态机"，
把"能不能跑"与"谁去跑"分开；执行侧对接留给 Worker 隔离实现。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from lumi_contracts.plugins import (
    ACTIVATABLE_PLUGIN_KINDS,
    IsolationLevel,
    PluginManifest,
    PluginSnapshot,
    PluginKind,
    isolation_for_trust,
    parse_plugin_kind,
)

from app.services.plugins.dependencies import (
    DependencyReport,
    DependencyResolver,
    InstalledPluginView,
)
from app.services.plugins.signatures import (
    SignatureOutcome,
    SignaturePolicy,
    log_outcome,
    policy_from_settings,
    verify_signature,
)
from app.services.plugins.state import (
    PluginStateStore,
    installation_record,
    kind_requires_developer_mode,
)


class PluginRejected(RuntimeError):
    """插件被拒绝（带稳定错误码，调用方据此返回 400/403 而不是 500）。"""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        self.code = str(code)
        self.message = str(message or code)
        self.details = dict(details or {})
        super().__init__(self.message)


#: 稳定拒绝码。
PLUGIN_MANIFEST_INVALID = "PLUGIN_MANIFEST_INVALID"
PLUGIN_KIND_UNKNOWN = "UNKNOWN_PLUGIN_KIND"
PLUGIN_KIND_NEEDS_DEV = "PLUGIN_KIND_REQUIRES_DEVELOPER_MODE"
PLUGIN_SIGNATURE_INVALID = "PLUGIN_SIGNATURE_INVALID"
PLUGIN_DEPENDENCIES_UNSATISFIED = "PLUGIN_DEPENDENCIES_UNSATISFIED"
PLUGIN_DEPLOYMENT_NOT_ALLOWED = "PLUGIN_DEPLOYMENT_NOT_ALLOWED"
PLUGIN_ISOLATION_TOO_WEAK = "PLUGIN_ISOLATION_TOO_WEAK"
PLUGIN_NOT_INSTALLED = "PLUGIN_NOT_INSTALLED"
PLUGIN_VERSION_UNAVAILABLE = "PLUGIN_VERSION_UNAVAILABLE"
PLUGIN_STATE_UNAVAILABLE = "PLUGIN_STATE_UNAVAILABLE"


@dataclass(slots=True)
class PluginInstallation:
    """一次安装的完整视图（API 与审计共用）。"""

    manifest: PluginManifest
    enabled: bool = False
    verified: bool = False
    signature_reason: str = ""
    dependencies: DependencyReport = field(default_factory=DependencyReport)
    previous_version: str = ""
    installed_at: float = 0.0
    updated_at: float = 0.0
    health_status: str = "unknown"
    health_detail: str = ""

    @property
    def plugin_id(self) -> str:
        return self.manifest.id

    @property
    def version(self) -> str:
        return self.manifest.version

    def to_api(self) -> dict[str, Any]:
        manifest = self.manifest
        return {
            "plugin_id": manifest.id,
            "name": manifest.name or manifest.id,
            "version": manifest.version,
            "kind": str(manifest.kind),
            "deployment": str(manifest.deployment),
            "trust_level": str(manifest.trust_level),
            "data_locality": str(manifest.data_locality),
            "isolation": str(manifest.isolation),
            "enabled": self.enabled,
            "verified": self.verified,
            "signature_reason": self.signature_reason,
            "previous_version": self.previous_version,
            "data_leaves_device": str(manifest.data_locality) != "local_only",
            "needs_local_confirmation": manifest.needs_approval,
            "needs_restart": manifest.deployment.value == "client",
            "needs_workspace_binding": any(
                str(item).startswith("workspace.") for item in manifest.provides.capabilities
            ),
            "permissions": [item.model_dump(mode="json") for item in manifest.permissions],
            "capabilities": list(manifest.provides.capabilities),
            "policies": list(manifest.provides.policies),
            "views": list(manifest.provides.views),
            "requires": manifest.requires.model_dump(mode="json"),
            "resource_limits": manifest.resource_limits.model_dump(mode="json"),
            "health_status": self.health_status,
            "health_detail": self.health_detail,
            "dependencies": self.dependencies.as_dict(),
            "digest": manifest.digest(),
            "installed_at": self.installed_at,
            "updated_at": self.updated_at,
        }

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "id": self.manifest.id,
            "version": self.manifest.version,
            "kind": str(self.manifest.kind),
            "enabled": self.enabled,
            "verified": self.verified,
            "digest": self.manifest.digest(),
        }


class PluginRegistry:
    """已安装插件的唯一权威（状态机 + 门禁 + 健康检查）。"""

    def __init__(
        self,
        *,
        store: PluginStateStore | None = None,
        resolver: DependencyResolver | None = None,
        signature_policy: SignaturePolicy | None = None,
        developer_mode: bool = False,
        capability_registry: Any = None,
        plugin_root: Path | None = None,
    ) -> None:
        self._store = store or PluginStateStore()
        self._resolver = resolver or DependencyResolver(registry=capability_registry)
        self._policy = signature_policy or policy_from_settings()
        self._developer_mode = bool(developer_mode)
        self._capability_registry = capability_registry
        self._plugin_root = Path(plugin_root) if plugin_root else self._store.path.parent.parent
        self._installations: dict[str, PluginInstallation] = {}
        self._load_from_state()

    # ── 读 ────────────────────────────────────────────────

    def all(self) -> list[PluginInstallation]:
        return sorted(self._installations.values(), key=lambda item: item.plugin_id)

    def enabled(self) -> list[PluginInstallation]:
        return [item for item in self.all() if item.enabled]

    def get(self, plugin_id: str) -> PluginInstallation | None:
        return self._installations.get(str(plugin_id))

    def require(self, plugin_id: str) -> PluginInstallation:
        found = self.get(plugin_id)
        if found is None:
            raise PluginRejected(PLUGIN_NOT_INSTALLED, f"插件未安装：{plugin_id}")
        return found

    def installed_views(self) -> list[InstalledPluginView]:
        return [
            InstalledPluginView(
                plugin_id=item.plugin_id,
                version=item.version,
                enabled=item.enabled,
                provides_capabilities=tuple(item.manifest.provides.capabilities),
                provides_policies=tuple(item.manifest.provides.policies),
            )
            for item in self.all()
        ]

    # ── 安装 / 升级 ───────────────────────────────────────

    @staticmethod
    def parse_manifest(payload: dict[str, Any]) -> PluginManifest:
        """解析 Manifest，把校验失败收敛成**稳定错误码**。

        未知插件类型在契约层就会失败（``PluginManifest`` 的校验器），这里把
        ``ValidationError`` 翻译成 ``UNKNOWN_PLUGIN_KIND`` / ``PLUGIN_MANIFEST_INVALID``，
        让 API 返回可行动的 400 而不是 500。
        """
        from pydantic import ValidationError

        try:
            return PluginManifest.model_validate(dict(payload or {}))
        except ValidationError as exc:
            text = str(exc)
            if "未知插件类型" in text:
                raise PluginRejected(
                    PLUGIN_KIND_UNKNOWN,
                    "未知插件类型（需要先注册 Extension Handler）",
                    details={"errors": text[:600]},
                ) from exc
            raise PluginRejected(
                PLUGIN_MANIFEST_INVALID,
                "插件 Manifest 不合法",
                details={"errors": text[:600]},
            ) from exc
        except ValueError as exc:
            raise PluginRejected(
                PLUGIN_MANIFEST_INVALID, f"插件 Manifest 不合法：{str(exc)[:300]}"
            ) from exc

    def install(
        self,
        manifest: PluginManifest,
        *,
        files: dict[str, bytes] | None = None,
        signature_policy: SignaturePolicy | None = None,
        activate: bool = True,
    ) -> PluginInstallation:
        """安装（或升级到）该 Manifest；默认安装后即启用。"""
        self._gate_kind(manifest)
        outcome = self._verify(manifest, files, signature_policy or self._policy)
        log_outcome(manifest.id, outcome)
        manifest = outcome.apply(manifest)
        self._gate_isolation(manifest)
        self._gate_deployment(manifest)
        report = self._resolver.resolve(manifest, installed=self._installed_views_excluding(manifest.id))
        if not report.ok:
            raise PluginRejected(
                PLUGIN_DEPENDENCIES_UNSATISFIED,
                "插件依赖未满足：" + "；".join(item.message for item in report.blocking[:4]),
                details={"issues": [item.to_dict() for item in report.blocking]},
            )
        now = time.time()
        existing = self._installations.get(manifest.id)
        health_status, health_detail = self.health_check(manifest)
        installation = PluginInstallation(
            manifest=manifest,
            enabled=bool(activate) and health_status != "unhealthy",
            verified=outcome.verified,
            signature_reason=outcome.reason,
            dependencies=report,
            previous_version=existing.version if existing else "",
            installed_at=existing.installed_at if existing else now,
            updated_at=now,
            health_status=health_status,
            health_detail=health_detail,
        )
        self._installations[manifest.id] = installation
        self._persist(installation)
        logger.info(
            "[plugin] {} {} 已安装（enabled={} verified={} kind={}）",
            manifest.id, manifest.version, installation.enabled, outcome.verified,
            manifest.kind,
        )
        return installation

    def enable(self, plugin_id: str) -> PluginInstallation:
        installation = self.require(plugin_id)
        report = self._resolver.resolve(
            installation.manifest, installed=self._installed_views_excluding(plugin_id)
        )
        if not report.ok:
            raise PluginRejected(
                PLUGIN_DEPENDENCIES_UNSATISFIED,
                "启用失败：依赖未满足：" + "；".join(item.message for item in report.blocking[:4]),
                details={"issues": [item.to_dict() for item in report.blocking]},
            )
        if installation.health_status == "unhealthy":
            raise PluginRejected(
                PLUGIN_STATE_UNAVAILABLE,
                f"启用失败：健康检查未通过（{installation.health_detail or '未知原因'}）",
            )
        installation.enabled = True
        installation.updated_at = time.time()
        self._persist(installation)
        return installation

    def disable(self, plugin_id: str, *, reason: str = "") -> PluginInstallation:
        """停用。被别的启用插件依赖时**拒绝**（否则会把它们一起打挂）。"""
        installation = self.require(plugin_id)
        dependents = self._dependents(plugin_id)
        if dependents:
            raise PluginRejected(
                PLUGIN_DEPENDENCIES_UNSATISFIED,
                f"停用失败：仍被启用的插件依赖：{', '.join(dependents)}",
                details={"dependents": dependents},
            )
        installation.enabled = False
        installation.updated_at = time.time()
        installation.health_detail = str(reason or installation.health_detail)
        self._persist(installation)
        return installation

    def upgrade(
        self,
        manifest: PluginManifest,
        *,
        files: dict[str, bytes] | None = None,
        signature_policy: SignaturePolicy | None = None,
    ) -> PluginInstallation:
        """升级：旧版本作为回滚点保留（``previous_version``）。"""
        existing = self.require(manifest.id)
        if existing.version == manifest.version:
            raise PluginRejected(
                PLUGIN_VERSION_UNAVAILABLE,
                f"插件已是版本 {manifest.version}；如需重装请先停用再安装",
            )
        return self.install(
            manifest, files=files, signature_policy=signature_policy, activate=existing.enabled
        )

    def rollback(self, plugin_id: str, *, version: str = "") -> PluginInstallation:
        """回滚到 ``previous_version``（或指定且已安装过的版本）。

        本阶段的回滚是"状态与版本指针回退 + 重新门禁"：没有代码分发就无法真的换代码，
        因此**不谎报**——只回退版本指针并重置健康状态，由调用方（或 Worker 侧）负责
        把对应版本的制品准备就绪。
        """
        installation = self.require(plugin_id)
        target = str(version or installation.previous_version or "")
        if not target:
            raise PluginRejected(
                PLUGIN_VERSION_UNAVAILABLE,
                f"插件 {plugin_id} 没有可回滚的版本（未发生过升级）",
            )
        if target == installation.version:
            return installation
        manifest = installation.manifest.model_copy(update={"version": target})
        previous = installation.version
        installation.manifest = manifest
        installation.previous_version = previous
        installation.updated_at = time.time()
        installation.enabled = False  # 回滚后必须重新启用（要求重新过门禁）
        installation.health_status, installation.health_detail = self.health_check(manifest)
        self._persist(installation)
        logger.warning("[plugin] {} 已回滚 {} → {}（需重新启用）", plugin_id, previous, target)
        return installation

    def uninstall(self, plugin_id: str) -> bool:
        installation = self.require(plugin_id)
        dependents = self._dependents(plugin_id)
        if dependents:
            raise PluginRejected(
                PLUGIN_DEPENDENCIES_UNSATISFIED,
                f"卸载失败：仍被启用的插件依赖：{', '.join(dependents)}",
                details={"dependents": dependents},
            )
        self._installations.pop(installation.plugin_id, None)
        self._store.drop(installation.plugin_id)
        logger.info("[plugin] {} 已卸载", plugin_id)
        return True

    # ── 健康检查 ──────────────────────────────────────────

    def health_check(self, manifest: PluginManifest) -> tuple[str, str]:
        """安装期/运行期的健康检查（按 ``healthcheck.kind``）。

        第一版只做**不改动系统**的检查：``none``（只看声明）、``import``（服务端插件
        声明的模块必须可解析；第三方模块默认不存在是正常情况，不阻断内置类型）、
        ``http``/``stdio`` 交给 Worker 侧（这里只记录"未探测"）。
        """
        check = manifest.healthcheck
        kind = str(check.kind or "none").strip().casefold()
        if kind in {"", "none"}:
            return "unknown", "未配置健康检查"
        if kind == "import":
            if manifest.trust_level.value in {"third_party", "local_dev"}:
                return "unknown", "第三方插件由 Worker 隔离加载，不在 API 进程内探测"
            target = str(check.target or manifest.entrypoints.module or "").strip()
            if not target:
                return "unknown", "未声明 module，跳过 import 健康检查"
            import importlib.util

            try:
                found = importlib.util.find_spec(target) is not None
            except (ImportError, ValueError, ModuleNotFoundError):
                found = False
            return ("healthy", f"模块可解析：{target}") if found else ("unhealthy", f"模块不可解析：{target}")
        if kind in {"http", "stdio"}:
            return "unknown", f"{kind} 健康检查由 Worker/客户端侧执行"
        return "unknown", f"未知健康检查类型：{kind}"

    def health(self, plugin_id: str) -> dict[str, Any]:
        installation = self.require(plugin_id)
        status, detail = self.health_check(installation.manifest)
        installation.health_status = status
        installation.health_detail = detail
        self._persist(installation)
        return {"plugin_id": plugin_id, "health_status": status, "detail": detail}

    # ── 快照 ──────────────────────────────────────────────

    def plugin_snapshot(self) -> dict[str, Any]:
        """启用中的插件快照（进 Job.run_view 的 ``plugin_snapshot``）。"""
        skills = [
            item
            for item in self.enabled()
            if item.manifest.kind is PluginKind.SKILL_PLUGIN
        ]
        providers = [
            item
            for item in self.enabled()
            if item.manifest.kind is PluginKind.CAPABILITY_PROVIDER
        ]
        policies = [
            item
            for item in self.enabled()
            if item.manifest.kind is PluginKind.POLICY_PACK
        ]
        return PluginSnapshot(
            skills=[
                _plugin_ref(item)
                for item in skills
            ],
            providers=[
                _provider_ref(item) for item in providers
            ],
            policies=[
                _policy_ref(item) for item in policies
            ],
        ).to_snapshot()

    # ── 内部 ──────────────────────────────────────────────

    def _installed_views_excluding(self, plugin_id: str) -> list[InstalledPluginView]:
        return [item for item in self.installed_views() if item.plugin_id != str(plugin_id)]

    def _dependents(self, plugin_id: str) -> list[str]:
        rows: list[str] = []
        for installation in self.all():
            if not installation.enabled or installation.plugin_id == plugin_id:
                continue
            for entry in installation.manifest.requires.plugins:
                from app.services.plugins.dependencies import parse_requirement

                name, _ = parse_requirement(entry)
                if name == plugin_id:
                    rows.append(installation.plugin_id)
                    break
        return rows

    def _gate_kind(self, manifest: PluginManifest) -> None:
        parsed = parse_plugin_kind(manifest.kind)
        if parsed is None:
            # 未知类型：生产默认拒绝（需要 Extension Handler 注册后才允许）。
            from app.services.plugins.extensions import extension_handlers

            if not extension_handlers.handles(manifest.kind):
                raise PluginRejected(
                    PLUGIN_KIND_UNKNOWN,
                    f"未知插件类型：{manifest.kind}（需要注册 Extension Handler）",
                    details={"kind": str(manifest.kind)},
                )
            if not self._developer_mode:
                raise PluginRejected(
                    PLUGIN_KIND_NEEDS_DEV,
                    "扩展类型插件只能在开发者模式安装",
                    details={"kind": str(manifest.kind)},
                )
            return
        if parsed.value not in ACTIVATABLE_PLUGIN_KINDS and not self._developer_mode:
            raise PluginRejected(
                PLUGIN_KIND_NEEDS_DEV,
                f"插件类型 {parsed.value} 只能在开发者模式安装",
                details={"kind": parsed.value},
            )

    def _gate_isolation(self, manifest: PluginManifest) -> None:
        required = isolation_for_trust(manifest.trust_level)
        order = [
            IsolationLevel.IN_PROCESS,
            IsolationLevel.RESTRICTED_WORKER,
            IsolationLevel.SANDBOXED,
            IsolationLevel.CLIENT_DEVICE,
        ]
        if manifest.deployment.value == "client":
            return
        if order.index(manifest.isolation) < order.index(required):
            raise PluginRejected(
                PLUGIN_ISOLATION_TOO_WEAK,
                f"隔离强度不足：trust={manifest.trust_level} 至少需要 {required}",
                details={"isolation": str(manifest.isolation), "required": str(required)},
            )

    def _gate_deployment(self, manifest: PluginManifest) -> None:
        if manifest.deployment.value == "local_dev" and not self._developer_mode:
            raise PluginRejected(
                PLUGIN_DEPLOYMENT_NOT_ALLOWED,
                "local_dev 部署只能在开发者模式使用（生产禁止任意本地路径加载）",
            )
        if manifest.deployment.value == "client":
            # 客户端插件由 Electron 安装；服务端只登记"客户端应提供什么"。
            if manifest.entrypoints.module:
                raise PluginRejected(
                    PLUGIN_DEPLOYMENT_NOT_ALLOWED, "客户端插件不得声明服务端 module 入口"
                )

    def _verify(
        self,
        manifest: PluginManifest,
        files: dict[str, bytes] | None,
        policy: SignaturePolicy,
    ) -> SignatureOutcome:
        outcome = verify_signature(manifest, policy=policy, files=files)
        if not outcome.verified and str(manifest.trust_level.value) in {"builtin", "official"}:
            if policy.require_signature_for_official and policy.configured:
                # 配了密钥却验不过 → 明确拒绝（不是"降级"，签名不符可能是被篡改）。
                raise PluginRejected(
                    PLUGIN_SIGNATURE_INVALID,
                    f"插件签名校验失败：{outcome.reason or '未知原因'}",
                    details={"algorithm": outcome.algorithm, "key_id": outcome.key_id},
                )
        return outcome

    def _persist(self, installation: PluginInstallation) -> None:
        self._store.put(
            installation_record(
                installation.manifest,
                enabled=installation.enabled,
                verified=installation.verified,
                now=installation.updated_at,
                previous_version=installation.previous_version,
                extra={
                    "health_status": installation.health_status,
                    "health_detail": installation.health_detail,
                    "signature_reason": installation.signature_reason,
                },
            )
        )

    def _load_from_state(self) -> None:
        """从状态文件恢复安装记录（重启后插件不会被"忘掉"）。

        恢复时**不重新验签**（签名结论已落库），但会重新跑健康检查——环境可能变了。
        """
        for row in self._store.all():
            try:
                manifest = _manifest_from_record(row)
            except Exception as exc:  # noqa: BLE001 - 坏记录只跳过它
                logger.warning(
                    "[plugin] 跳过坏安装记录 {}: {}",
                    str(row.get("plugin_id"))[:60], str(exc)[:120],
                )
                continue
            status, detail = self.health_check(manifest)
            self._installations[manifest.id] = PluginInstallation(
                manifest=manifest,
                enabled=bool(row.get("enabled")),
                verified=bool(row.get("verified")),
                signature_reason=str(row.get("signature_reason") or ""),
                previous_version=str(row.get("previous_version") or ""),
                installed_at=float(row.get("installed_at") or 0.0),
                updated_at=float(row.get("updated_at") or 0.0),
                health_status=status if status != "unhealthy" else "unhealthy",
                health_detail=detail or str(row.get("health_detail") or ""),
            )


def _plugin_ref(installation: PluginInstallation) -> Any:
    from lumi_contracts.plugins import PluginRef

    return PluginRef(
        id=installation.plugin_id,
        version=installation.version,
        kind=str(installation.manifest.kind),
        deployment=str(installation.manifest.deployment),
        digest=installation.manifest.digest(),
        trust_level=str(installation.manifest.trust_level),
    )


def _provider_ref(installation: PluginInstallation) -> Any:
    from lumi_contracts.plugins import ProviderRef

    return ProviderRef(
        id=installation.plugin_id,
        version=installation.version,
        deployment=str(installation.manifest.deployment),
        plugin_id=installation.plugin_id,
    )


def _policy_ref(installation: PluginInstallation) -> Any:
    from lumi_contracts.plugins import PolicyRef

    return PolicyRef(id=installation.plugin_id, version=installation.version, source="plugin")


def _manifest_from_record(row: dict[str, Any]) -> PluginManifest:
    """安装记录 → Manifest（只恢复门禁与审计需要的字段）。"""
    requires = row.get("requires") or {}
    provides = row.get("provides") or {}
    return PluginManifest(
        id=str(row.get("plugin_id") or ""),
        version=str(row.get("version") or ""),
        kind=str(row.get("kind") or ""),
        deployment=str(row.get("deployment") or "server"),
        data_locality=str(row.get("data_locality") or "local_only"),
        trust_level=str(row.get("trust_level") or "third_party"),
        requires=dict(requires),
        provides=dict(provides),
        isolation=str(
            row.get("isolation")
            or isolation_for_trust(str(row.get("trust_level") or "third_party"))
        ),
    )


__all__ = [
    "PLUGIN_DEPENDENCIES_UNSATISFIED",
    "PLUGIN_DEPLOYMENT_NOT_ALLOWED",
    "PLUGIN_ISOLATION_TOO_WEAK",
    "PLUGIN_KIND_NEEDS_DEV",
    "PLUGIN_KIND_UNKNOWN",
    "PLUGIN_MANIFEST_INVALID",
    "PLUGIN_NOT_INSTALLED",
    "PLUGIN_SIGNATURE_INVALID",
    "PLUGIN_STATE_UNAVAILABLE",
    "PLUGIN_VERSION_UNAVAILABLE",
    "PluginInstallation",
    "PluginRejected",
    "PluginRegistry",
    "installation_record",
    "kind_requires_developer_mode",
]
