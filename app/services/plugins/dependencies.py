"""阶段 4：插件依赖解析（依赖缺失**不污染系统**，且在安装期就报出来）。

解析顺序与报告形状都是稳定的：

1. ``requires.capabilities``：能力必须在能力目录里声明；并给出"当前有没有 Provider"
   的可用性（缺 Provider 不算安装失败——能力可能是稍后由客户端注册的）；
2. ``requires.plugins``：被依赖插件必须已安装且**启用**，版本需满足 ``>=`` 约束；
3. ``requires.policies``：策略包必须存在（阶段 5 的 Policy Pack 注册后可见）；
4. ``requires.min_lumi_version``：与本服务版本比较（``0.1.0``）。

报告里的 ``issues`` 每条带 ``code``/``required``/``message``，安装器据此决定拒绝还是
警告：``required=True`` 的问题让安装失败，可选问题只记录（降级运行）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lumi_contracts.plugins import PluginManifest

#: 稳定问题码（前端按它给"去装/去启用"的入口）。
PLUGIN_REQUIRES_UNKNOWN = "PLUGIN_REQUIRES_UNKNOWN"
CAPABILITY_UNKNOWN = "CAPABILITY_UNKNOWN"
CAPABILITY_NO_PROVIDER = "CAPABILITY_NO_PROVIDER"
PLUGIN_MISSING = "PLUGIN_MISSING"
PLUGIN_DISABLED = "PLUGIN_DISABLED"
PLUGIN_VERSION_MISMATCH = "PLUGIN_VERSION_MISMATCH"
POLICY_UNKNOWN = "POLICY_UNKNOWN"
LUMI_VERSION_TOO_OLD = "LUMI_VERSION_TOO_OLD"

VERSION = "0.1.0"


@dataclass(slots=True)
class DependencyIssue:
    code: str
    message: str
    required: bool = True
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "required": bool(self.required),
            "details": dict(self.details),
        }


@dataclass(slots=True)
class DependencyReport:
    """一次依赖解析的结论（``state`` 与既有 ``dependencies.DependencyReport`` 对齐）。"""

    plugin_id: str = ""
    issues: list[DependencyIssue] = field(default_factory=list)
    capability_availability: dict[str, bool] = field(default_factory=dict)

    @property
    def blocking(self) -> list[DependencyIssue]:
        return [item for item in self.issues if item.required]

    @property
    def state(self) -> str:
        if any(item.code == PLUGIN_REQUIRES_UNKNOWN for item in self.issues):
            return "invalid"
        if self.blocking:
            return "unavailable"
        if self.issues:
            return "degraded"
        return "available"

    @property
    def ok(self) -> bool:
        return not self.blocking

    def as_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "state": self.state,
            "ok": self.ok,
            "issues": [item.to_dict() for item in self.issues],
            "capability_availability": dict(self.capability_availability),
        }


def parse_requirement(entry: str) -> tuple[str, str]:
    """``name>=1.2.0`` → ``(name, "1.2.0")``；无约束返回 ``(name, "")``。"""
    text = str(entry or "").strip()
    for operator in (">=", "==", "="):
        if operator in text:
            name, _, version = text.partition(operator)
            return name.strip(), version.strip()
    return text, ""


def version_tuple(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in str(value or "").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if digits:
            parts.append(int(digits))
    return tuple(parts)


def version_satisfies(actual: str, required: str) -> bool:
    """只支持 ``>=`` 语义（与既有插件准入一致，不做完整 semver 区间）。"""
    if not required:
        return True
    return version_tuple(actual) >= version_tuple(required)


@dataclass(slots=True)
class InstalledPluginView:
    """解析器需要的"已安装插件"最小视图（避免依赖安装器实现）。"""

    plugin_id: str
    version: str
    enabled: bool
    provides_capabilities: tuple[str, ...] = ()
    provides_policies: tuple[str, ...] = ()


class DependencyResolver:
    """插件依赖解析（能力 / 插件 / 策略 / 版本）。"""

    def __init__(
        self,
        *,
        catalog: Any = None,
        registry: Any = None,
        policy_ids: set[str] | None = None,
        lumi_version: str = VERSION,
    ) -> None:
        self._catalog = catalog
        self._registry = registry
        self._policy_ids = set(policy_ids or ())
        self._lumi_version = str(lumi_version or VERSION)

    def _catalog_obj(self) -> Any:
        if self._catalog is not None:
            return self._catalog
        from app.agents.capabilities.catalog import capability_catalog

        return capability_catalog

    def set_policy_ids(self, policy_ids: set[str]) -> None:
        self._policy_ids = set(policy_ids or ())

    def resolve(
        self,
        manifest: PluginManifest,
        *,
        installed: list[InstalledPluginView] | list[dict[str, Any]] | None = None,
    ) -> DependencyReport:
        report = DependencyReport(plugin_id=manifest.id)
        catalog = self._catalog_obj()
        # 1) 能力
        for entry in manifest.requires.capabilities:
            name, version_text = parse_requirement(entry)
            descriptor = catalog.get(name, version=int(version_text) if version_text.isdigit() else None)
            report.capability_availability[entry] = False
            if descriptor is None:
                report.issues.append(
                    DependencyIssue(
                        code=CAPABILITY_UNKNOWN,
                        message=f"依赖的能力未声明：{entry}",
                        details={"capability": entry},
                    )
                )
                continue
            providers = self._providers_for(descriptor.name, descriptor.contract_version)
            report.capability_availability[entry] = bool(providers)
            if not providers:
                # 可选依赖：能力由客户端稍后注册，不算安装失败。
                report.issues.append(
                    DependencyIssue(
                        code=CAPABILITY_NO_PROVIDER,
                        message=f"{entry} 暂时没有可用 Provider（客户端未连接）",
                        required=False,
                        details={"capability": entry},
                    )
                )
        # 2) 插件
        rows = {self._view_id(item): self._view(item) for item in (installed or [])}
        for entry in manifest.requires.plugins:
            name, required_version = parse_requirement(entry)
            target = rows.get(name)
            if target is None:
                report.issues.append(
                    DependencyIssue(
                        code=PLUGIN_MISSING,
                        message=f"缺少依赖插件：{name}",
                        details={"plugin_id": name},
                    )
                )
                continue
            if not target.enabled:
                report.issues.append(
                    DependencyIssue(
                        code=PLUGIN_DISABLED,
                        message=f"依赖插件未启用：{name}",
                        details={"plugin_id": name},
                    )
                )
            if not version_satisfies(target.version, required_version):
                report.issues.append(
                    DependencyIssue(
                        code=PLUGIN_VERSION_MISMATCH,
                        message=(
                            f"依赖插件版本不满足：{name} 需要 >= {required_version}，"
                            f"当前 {target.version}"
                        ),
                        details={"plugin_id": name, "required": required_version},
                    )
                )
        # 3) 策略包
        for entry in manifest.requires.policies:
            name, _ = parse_requirement(entry)
            known = name in self._policy_ids or any(
                name in target.provides_policies for target in rows.values()
            )
            if not known:
                report.issues.append(
                    DependencyIssue(
                        code=POLICY_UNKNOWN,
                        message=f"依赖的策略包不存在：{name}",
                        required=False,
                        details={"policy_id": name},
                    )
                )
        # 4) 版本
        if manifest.requires.min_lumi_version and not version_satisfies(
            self._lumi_version, manifest.requires.min_lumi_version
        ):
            report.issues.append(
                DependencyIssue(
                    code=LUMI_VERSION_TOO_OLD,
                    message=(
                        f"需要 Lumi >= {manifest.requires.min_lumi_version}，"
                        f"当前 {self._lumi_version}"
                    ),
                    details={"required": manifest.requires.min_lumi_version},
                )
            )
        return report

    def _providers_for(self, name: str, version: int) -> list[Any]:
        if self._registry is None:
            return []
        try:
            return list(self._registry.registrations(name, version=version))
        except Exception:  # noqa: BLE001 - 解析失败按"没有 Provider"处理
            return []

    @staticmethod
    def _view_id(item: Any) -> str:
        if isinstance(item, dict):
            return str(item.get("plugin_id") or item.get("id") or "")
        return str(getattr(item, "plugin_id", "") or "")

    @staticmethod
    def _view(item: Any) -> InstalledPluginView:
        if isinstance(item, InstalledPluginView):
            return item
        if isinstance(item, dict):
            provides = item.get("provides") or {}
            return InstalledPluginView(
                plugin_id=str(item.get("plugin_id") or item.get("id") or ""),
                version=str(item.get("version") or ""),
                enabled=bool(item.get("enabled")),
                provides_capabilities=tuple(
                    str(entry) for entry in (provides.get("capabilities") or [])
                ),
                provides_policies=tuple(str(entry) for entry in (provides.get("policies") or [])),
            )
        return InstalledPluginView(
            plugin_id=str(getattr(item, "plugin_id", "") or ""),
            version=str(getattr(item, "version", "") or ""),
            enabled=bool(getattr(item, "enabled", False)),
        )


__all__ = [
    "CAPABILITY_NO_PROVIDER",
    "CAPABILITY_UNKNOWN",
    "DependencyIssue",
    "DependencyReport",
    "DependencyResolver",
    "InstalledPluginView",
    "LUMI_VERSION_TOO_OLD",
    "PLUGIN_DISABLED",
    "PLUGIN_MISSING",
    "PLUGIN_REQUIRES_UNKNOWN",
    "PLUGIN_VERSION_MISMATCH",
    "POLICY_UNKNOWN",
    "VERSION",
    "parse_requirement",
    "version_satisfies",
    "version_tuple",
]
