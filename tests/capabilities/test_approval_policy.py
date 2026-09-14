"""统一审批策略引擎与工作区能力分档的回归测试。"""

from __future__ import annotations

import asyncio

from app.agents.skills.approval_policy import (
    APPROVAL_MODE_AUTO,
    APPROVAL_MODE_CONFIRM,
    should_confirm,
)
from app.workspace import context as wc


def test_stage_write_is_auto_in_both_modes():
    for mode in (APPROVAL_MODE_AUTO, APPROVAL_MODE_CONFIRM):
        decision = should_confirm(
            tool="mcp__lumi_pc__workspace_stage_write",
            arguments={"path": "src/main.py", "content": "x"},
            workspace_context={"workspace_available": True, "approval_mode": mode},
        )
        assert decision.decision == "allow"
        assert decision.tier == "auto"


def test_read_tools_auto_when_workspace_available():
    decision = should_confirm(
        tool="mcp__lumi_pc__workspace_read",
        arguments={"path": "README.md"},
        workspace_context={"workspace_available": True, "approval_mode": APPROVAL_MODE_CONFIRM},
    )
    assert decision.decision == "allow"


def test_read_denied_when_workspace_unavailable():
    decision = should_confirm(
        tool="mcp__lumi_pc__workspace_read",
        arguments={"path": "README.md"},
        workspace_context={"workspace_available": False},
    )
    assert decision.decision == "deny"


def test_workspace_tool_denied_when_workspace_unavailable():
    decision = should_confirm(
        tool="mcp__lumi_pc__workspace_commit",
        arguments={},
        workspace_context={"workspace_available": False},
    )
    assert decision.decision == "deny"


def test_commit_follows_approval_mode():
    # “帮我确认”关闭：提交前需要一次性确认。
    closed = should_confirm(
        tool="mcp__lumi_pc__workspace_commit",
        arguments={"workspace_id": "ws1"},
        workspace_context={"workspace_available": True, "approval_mode": APPROVAL_MODE_CONFIRM},
    )
    assert closed.decision == "require_confirmation"
    assert closed.scope == "task"
    # 已在该任务确认过一次 → 放行。
    approved = should_confirm(
        tool="mcp__lumi_pc__workspace_commit",
        arguments={},
        workspace_context={"workspace_available": True},
        execution_grant={"approval_mode": APPROVAL_MODE_CONFIRM, "approved": True},
    )
    assert approved.decision == "allow"
    # “帮我确认”开启：普通提交自动执行。
    auto = should_confirm(
        tool="mcp__lumi_pc__workspace_commit",
        arguments={},
        workspace_context={"workspace_available": True, "approval_mode": APPROVAL_MODE_AUTO},
    )
    assert auto.decision == "allow"


def test_rollback_always_confirmed_even_in_auto_mode():
    decision = should_confirm(
        tool="mcp__lumi_pc__workspace_rollback",
        arguments={},
        workspace_context={"workspace_available": True, "approval_mode": APPROVAL_MODE_AUTO},
    )
    assert decision.decision == "require_confirmation"
    assert decision.tier == "critical"
    assert decision.scope == "workspace"


def test_root_delete_is_critical():
    decision = should_confirm(
        tool="mcp__lumi_pc__workspace_stage_delete",
        arguments={"path": "", "recursive": True},
        workspace_context={"workspace_available": True, "approval_mode": APPROVAL_MODE_AUTO},
    )
    assert decision.decision == "require_confirmation"
    assert decision.tier == "critical"


def test_dangerous_command_in_sandbox_is_critical():
    decision = should_confirm(
        tool="mcp__lumi_pc__sandbox_run",
        arguments={"command": "git reset --hard HEAD"},
        workspace_context={"workspace_available": True, "approval_mode": APPROVAL_MODE_AUTO},
    )
    assert decision.decision == "require_confirmation"
    assert decision.tier == "critical"


def test_secret_path_read_is_critical():
    decision = should_confirm(
        tool="mcp__lumi_pc__workspace_read",
        arguments={"path": ".env"},
        workspace_context={"workspace_available": True, "approval_mode": APPROVAL_MODE_AUTO},
    )
    assert decision.decision == "require_confirmation"
    assert decision.tier == "critical"


def test_routine_sandbox_command_auto_in_auto_mode():
    decision = should_confirm(
        tool="mcp__lumi_pc__sandbox_run",
        arguments={"command": "pytest -q"},
        workspace_context={"workspace_available": True, "approval_mode": APPROVAL_MODE_AUTO},
    )
    assert decision.decision == "allow"


def test_expired_snapshot_downgrades_auto_to_confirm():
    decision = should_confirm(
        tool="mcp__lumi_pc__workspace_commit",
        arguments={},
        workspace_context={
            "workspace_available": True,
            "approval_mode": APPROVAL_MODE_AUTO,
            "expires_at": "2000-01-01T00:00:00Z",
        },
    )
    assert decision.decision == "require_confirmation"
    assert "过期" in decision.reason


def test_future_snapshot_keeps_auto_mode():
    decision = should_confirm(
        tool="mcp__lumi_pc__workspace_commit",
        arguments={},
        workspace_context={
            "workspace_available": True,
            "approval_mode": APPROVAL_MODE_AUTO,
            "expires_at": "2999-01-01T00:00:00Z",
        },
    )
    assert decision.decision == "allow"


def test_expired_snapshot_still_allows_auto_tier_reads():
    decision = should_confirm(
        tool="mcp__lumi_pc__workspace_read",
        arguments={"path": "README.md"},
        workspace_context={
            "workspace_available": True,
            "approval_mode": APPROVAL_MODE_AUTO,
            "expires_at": "2000-01-01T00:00:00Z",
        },
    )
    assert decision.decision == "allow"


def test_workspace_context_exposes_full_capability_groups(tmp_path, monkeypatch):
    monkeypatch.setattr(workspaces_module(), "ROOT", tmp_path / "workspaces")
    monkeypatch.setattr(settings_module(), "MCP_SERVERS", [{
        "name": "lumi_pc", "url": "http://pc/mcp", "provider_type": "desktop_mcp",
        "user_id": "u1", "device_id": "dev1",
    }])
    created = _create(monkeypatch, tmp_path)
    wsid = created["workspace_id"]
    advertised = sorted(wc.WORKSPACE_ALL_CAPABILITIES)
    monkeypatch.setattr(wc, "_server_is_healthy", _ok())
    monkeypatch.setattr(wc, "_advertised_tools", _names(advertised))
    monkeypatch.setattr(wc, "_cache_write", _noop_write())
    monkeypatch.setattr(wc, "_cache_read", _noop_read())

    async def fake_electron(_server, tool_name, _args, **_kw):
        if tool_name == "workspace_diff":
            return {"status": "success", "data": {"base_version": 1}}
        return {"status": "success", "data": {
            "version": 1,
            "entries": [{"path": "src", "type": "directory"}],
            "permission": {"approval_mode": "auto_routine", "policy_version": "3", "issued_at": "t1", "expires_at": "t2"},
        }}

    monkeypatch.setattr(wc, "_call_electron", fake_electron)
    ctx = asyncio.run(wc.load_workspace_context("u1", workspace_id=wsid))
    assert ctx.available is True
    assert ctx.approval_mode == "auto_routine"
    assert ctx.policy_version == "3"
    groups = ctx.capability_groups
    assert "workspace_read" in groups["read"]
    assert "workspace_stat" in groups["read"]
    assert "workspace_stage_write" in groups["stage_write"]
    assert "sandbox_run" in groups["sandbox"]
    assert "workspace_commit" in groups["commit"]
    profile = wc.workspace_permission_profile(ctx)
    assert profile["access_level"] == "full"
    assert set(profile["available_domains"]) == {"workspace_read", "workspace_write", "sandbox_execution"}


def test_workspace_context_reads_electron_permission_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(workspaces_module(), "ROOT", tmp_path / "workspaces")
    monkeypatch.setattr(settings_module(), "MCP_SERVERS", [{
        "name": "lumi_pc", "url": "http://pc/mcp", "provider_type": "desktop_mcp",
        "user_id": "u1", "device_id": "dev1",
    }])
    created = _create(monkeypatch, tmp_path)
    monkeypatch.setattr(wc, "_server_is_healthy", _ok())
    monkeypatch.setattr(wc, "_advertised_tools", _names(sorted(wc.WORKSPACE_ALL_CAPABILITIES)))
    monkeypatch.setattr(wc, "_cache_write", _noop_write())
    monkeypatch.setattr(wc, "_cache_read", _noop_read())

    async def fake_electron(_server, tool_name, _args, **_kw):
        if tool_name == "workspace_diff":
            return {"status": "success", "data": {"base_version": 1}}
        return {"status": "success", "data": {
            "workspace_version": 1,
            "top_level": [{"path": "README.md", "type": "file"}],
            "permission_profile": {
                "access_level": "full",
                "approval_mode": "auto_routine",
                "policy_version": 7,
            },
        }}

    monkeypatch.setattr(wc, "_call_electron", fake_electron)
    ctx = asyncio.run(wc.load_workspace_context("u1", workspace_id=created["workspace_id"]))
    assert ctx.approval_mode == "auto_routine"
    assert ctx.policy_version == "7"
    assert ctx.entries == [{"path": "README.md", "type": "file", "size": None}]


def test_workspace_context_accepts_catalog_full_tool_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(workspaces_module(), "ROOT", tmp_path / "workspaces")
    monkeypatch.setattr(settings_module(), "MCP_SERVERS", [{
        "name": "lumi_pc", "url": "http://pc/mcp", "provider_type": "desktop_mcp",
        "user_id": "u1", "device_id": "dev1",
    }])
    created = _create(monkeypatch, tmp_path)
    monkeypatch.setattr(wc, "_server_is_healthy", _ok())
    monkeypatch.setattr(wc, "_advertised_tools", _names(sorted(wc.WORKSPACE_ALL_CAPABILITIES)))
    monkeypatch.setattr(wc, "_cache_write", _noop_write())
    monkeypatch.setattr(wc, "_cache_read", _noop_read())

    async def fake_electron(_server, tool_name, _args, **_kw):
        if tool_name == "workspace_diff":
            return {"status": "success", "data": {"base_version": 1}}
        return {"status": "success", "data": {
            "workspace_version": 1,
            "tools": sorted(wc.WORKSPACE_ALL_CAPABILITIES),
            "capability_groups": {
                "read": sorted(wc.WORKSPACE_READ_CAPABILITIES),
                "stage_write": sorted(wc.WORKSPACE_STAGE_WRITE_CAPABILITIES),
                "sandbox": sorted(wc.WORKSPACE_SANDBOX_CAPABILITIES),
                "commit": sorted(wc.WORKSPACE_COMMIT_CAPABILITIES),
            },
            "permission_profile": {"access_level": "full", "approval_mode": "manual_commit"},
        }}

    monkeypatch.setattr(wc, "_call_electron", fake_electron)
    ctx = asyncio.run(wc.load_workspace_context("u1", workspace_id=created["workspace_id"]))
    assert ctx.available is True
    # ``capabilities`` is the model's initial read window; the full catalog is
    # exposed through stage-specific groups and injected only when needed.
    assert set(ctx.capabilities) == set(wc.WORKSPACE_READ_CAPABILITIES)
    grouped = set().union(*(set(values) for values in ctx.capability_groups.values()))
    assert grouped == set(wc.WORKSPACE_ALL_CAPABILITIES)


def workspaces_module():
    from app.workspace import service

    return service


def settings_module():
    from app.core.config import settings

    return settings


def _create(monkeypatch, tmp_path):
    created = workspaces_module().create_workspace("u1", "项目", "conv-1")
    workspaces_module().register_workspace_device(
        "u1", created["workspace_id"], device_id="dev1", device_server="lumi_pc"
    )
    return created


def _ok():
    async def _healthy(_s):
        return True

    return _healthy


def _names(values):
    async def _advertised(_s):
        return list(values)

    return _advertised


def _noop_write():
    async def _write(_wsid, _payload):
        return None

    return _write


def _noop_read():
    async def _read(_wsid):
        return None

    return _read
