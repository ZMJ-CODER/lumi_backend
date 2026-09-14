"""Workflow Skill dependency resolution and runtime capability diagnostics.

This module is deliberately generic: it does not infer business intent or
contain route-specific rules.  It only compares a Skill manifest with the
capability snapshot already authorized for the current job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DependencyIssue:
    name: str
    code: str
    message: str
    required: bool = True


@dataclass
class DependencyReport:
    state: str = "available"
    issues: list[DependencyIssue] = field(default_factory=list)

    @property
    def required_issues(self) -> list[DependencyIssue]:
        return [item for item in self.issues if item.required]

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "missing": [item.name for item in self.issues],
            "issues": [
                {"name": item.name, "code": item.code, "message": item.message, "required": item.required}
                for item in self.issues
            ],
        }


def _version(value: str) -> tuple[int, int, int]:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(value or "0.0.0"))
    return tuple(int(part) for part in match.groups()) if match else (0, 0, 0)


# ── 聚合入口的依赖别名 ──────────────────────────────────────
# 模型只声明聚合读取入口 workspace_navigator，但它由后端合成，Electron 的工具
# 清单里并不存在同名工具。依赖解析必须把它当作“只要该桌面提供了任一内部原子读取
# 能力即可用”，否则工作流会误报 MISSING_TOOL。
# 原子名取自 workspace_context（唯一事实来源），不在这里维护第二份字面量。
def _aggregated_sources() -> dict[str, tuple[str, ...]]:
    from app.workspace.context import (
        WORKSPACE_INTERNAL_READ_CAPABILITIES,
        WORKSPACE_NAVIGATOR,
    )

    return {WORKSPACE_NAVIGATOR: tuple(sorted(WORKSPACE_INTERNAL_READ_CAPABILITIES))}


AGGREGATED_DEPENDENCY_SOURCES: dict[str, tuple[str, ...]] = _aggregated_sources()


def _qualified(server_name: str, raw_name: str) -> str:
    return f"mcp__{server_name}__{raw_name}"


def synthesize_aggregated_capabilities(
    capabilities: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """由内部原子能力推导聚合入口能力（不修改输入）。"""
    if not capabilities:
        return {}
    servers: set[str] = set()
    for name in capabilities:
        parts = str(name).split("__", 2)
        if len(parts) == 3 and parts[0] == "mcp":
            servers.add(parts[1])
    derived: dict[str, dict[str, Any]] = {}
    for server_name in servers:
        for aggregated, sources in AGGREGATED_DEPENDENCY_SOURCES.items():
            target = _qualified(server_name, aggregated)
            if target in capabilities:
                continue
            for raw in sources:
                source = capabilities.get(_qualified(server_name, raw))
                if source is None:
                    continue
                minimum = _version(str(source.get("version") or "1.0.0"))
                # 聚合入口的可用版本不低于它所复用的原子能力版本。
                derived[target] = {
                    "version": f"{minimum[0]}.{minimum[1]}.{minimum[2]}",
                    "provider": source.get("provider") or "desktop_mcp",
                    "environment": source.get("environment") or "client",
                    "annotations": {**dict(source.get("annotations") or {}), "aggregated": True},
                }
                break
    return derived


def resolve_dependencies(
    manifest: dict[str, Any] | None,
    capabilities: dict[str, dict[str, Any]],
    *,
    execution_scope: str = "backend",
) -> DependencyReport:
    """Compare declared dependencies with a frozen authorized capability map."""
    report = DependencyReport()
    manifest = manifest if isinstance(manifest, dict) else {}
    rows = manifest.get("tools") or []
    if not isinstance(rows, list):
        report.issues.append(DependencyIssue("dependencies.tools", "MANIFEST_INVALID", "dependencies.tools 必须是数组"))
        report.state = "invalid"
        return report
    for row in rows:
        if not isinstance(row, dict):
            report.issues.append(DependencyIssue("dependencies.tools", "MANIFEST_INVALID", "工具依赖项必须是对象"))
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            report.issues.append(DependencyIssue("", "MANIFEST_INVALID", "工具依赖缺少 name"))
            continue
        required = bool(row.get("required", True))
        capability = capabilities.get(name)
        if capability is None:
            report.issues.append(DependencyIssue(name, "MISSING_TOOL", f"依赖工具 {name} 未注册、未授权或当前不可用", required))
            continue
        minimum = _version(str(row.get("min_version") or "0.0.0"))
        actual = _version(str(capability.get("version") or "0.0.0"))
        if actual < minimum:
            report.issues.append(DependencyIssue(name, "VERSION_MISMATCH", f"依赖工具 {name} 版本低于要求 {row.get('min_version')}", required))
            continue
        provider = str(row.get("provider") or "any")
        actual_provider = str(capability.get("provider") or capability.get("environment") or "")
        if provider != "any" and actual_provider not in {provider, "client" if provider == "desktop_mcp" else provider}:
            report.issues.append(DependencyIssue(name, "PROVIDER_MISMATCH", f"依赖工具 {name} 不由 {provider} 提供", required))
            continue
        availability = str((capability.get("annotations") or {}).get("availability_hint") or "available")
        if availability not in {"available", "online", ""}:
            code = "CLIENT_OFFLINE" if availability in {"offline", "circuit_breaker"} else "PROVIDER_UNAVAILABLE"
            report.issues.append(DependencyIssue(name, code, f"依赖工具 {name} 当前不可用（{availability}）", required))
    if report.required_issues:
        report.state = "unavailable"
    elif report.issues:
        report.state = "degraded"
    return report

