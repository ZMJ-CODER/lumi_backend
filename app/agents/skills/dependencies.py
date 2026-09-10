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

