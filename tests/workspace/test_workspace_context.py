"""WorkspaceContext / workspace_catalog 支持与设备路由的回归测试。

测试替换 Electron MCP seam（workspace_context._call_electron 等），
不依赖真实 Redis / Electron 连接。
"""

from __future__ import annotations

import asyncio

from app.agents.mcp import desktop_connections as registry_module
from app.agents.skills import executor as executor_module
from app.workspace import context as wc
from app.workspace import service as workspaces

MCP_ROWS = [
    {"name": "lumi_pc", "url": "http://pc/mcp", "provider_type": "desktop_mcp",
     "user_id": "u1", "device_id": "dev1"},
]


def _install_workspace(tmp_path, monkeypatch, *, user="u1", name="项目", conversation="conv-1",
                       device_id="dev1", device_server="lumi_pc"):
    monkeypatch.setattr(workspaces, "ROOT", tmp_path / "workspaces")
    monkeypatch.setattr(registry_module.settings, "MCP_SERVERS", [dict(row) for row in MCP_ROWS])
    created = workspaces.create_workspace(user, name, conversation)
    if device_id:
        workspaces.register_workspace_device(
            user, created["workspace_id"],
            device_id=device_id, device_server=device_server,
        )
    return created


def _wsid_of(tmp_path) -> str:
    return workspaces.list_workspaces("u1")[0]["workspace_id"]


def _fake_electron(*, diff_version=3, catalog_version=3, catalog_entries=None,
                   catalog_staged=None, diff_status="success", catalog_status="success"):
    async def _call(server_name, tool_name, args, **_kwargs):
        if tool_name == "workspace_diff":
            if diff_status != "success":
                return {"status": "failed", "error_code": diff_status}
            return {"status": "success", "data": {"base_version": diff_version}}
        if tool_name == "workspace_catalog":
            if catalog_status != "success":
                return {"status": "failed", "error_code": catalog_status}
            return {
                "status": "success",
                "data": {
                    "version": catalog_version,
                    "entries": catalog_entries or [],
                    "staged_changes": catalog_staged or [],
                },
            }
        if tool_name == "workspace_list":
            return {"status": "success", "data": {"items": catalog_entries or []}}
        return {"status": "failed", "error_code": "WORKSPACE_READ_FAILED"}
    return _call


async def _healthy(_server_name: str) -> bool:
    return True


async def _offline(_server_name: str) -> bool:
    return False


async def _advertised(_server_name: str) -> list[str]:
    return ["workspace_catalog", "workspace_list", "workspace_read", "workspace_search", "workspace_diff"]


async def _only_catalog(_server_name: str) -> list[str]:
    return ["workspace_catalog"]


async def _no_cache_read(_workspace_id: str):
    return None


async def _no_cache_write(_workspace_id: str, _payload: dict):
    return None


def test_not_bound_when_conversation_has_no_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(workspaces, "ROOT", tmp_path / "workspaces")
    ctx = asyncio.run(wc.load_workspace_context("u1", conversation_id="missing"))
    assert ctx.status_code == wc.WORKSPACE_NOT_BOUND
    assert ctx.available is False
    assert not ctx.entries


def test_not_registered_when_workspace_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(workspaces, "ROOT", tmp_path / "workspaces")
    ctx = asyncio.run(wc.load_workspace_context("u1", workspace_id="nope"))
    assert ctx.status_code == wc.WORKSPACE_NOT_REGISTERED


def test_not_registered_when_no_route_to_desktop(tmp_path, monkeypatch):
    _install_workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(registry_module.settings, "MCP_SERVERS", [])
    ctx = asyncio.run(wc.load_workspace_context("u1", workspace_id=_wsid_of(tmp_path)))
    assert ctx.status_code == wc.WORKSPACE_NOT_REGISTERED
    assert ctx.available is False


def test_device_offline_when_server_down(tmp_path, monkeypatch):
    _install_workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(wc, "_server_is_healthy", _offline)
    ctx = asyncio.run(wc.load_workspace_context("u1", workspace_id=_wsid_of(tmp_path)))
    assert ctx.status_code == wc.WORKSPACE_DEVICE_OFFLINE
    assert ctx.available is False


def test_catalog_loaded_and_summary_rendered(tmp_path, monkeypatch):
    created = _install_workspace(tmp_path, monkeypatch)
    wsid = created["workspace_id"]
    entries = [
        {"path": "src", "type": "directory"},
        {"path": "README.md", "type": "file", "size": 3200},
    ]
    monkeypatch.setattr(wc, "_server_is_healthy", _healthy)
    monkeypatch.setattr(wc, "_advertised_tools", _advertised)
    monkeypatch.setattr(wc, "_call_electron", _fake_electron(
        catalog_entries=entries,
        catalog_staged=[{"path": "README.md", "kind": "modified"}],
    ))
    ctx = asyncio.run(wc.load_workspace_context("u1", workspace_id=wsid))
    assert ctx.available is True
    assert ctx.status_code == wc.WORKSPACE_READY
    assert ctx.version == 3
    assert [e["path"] for e in ctx.entries] == ["src", "README.md"]
    assert len(ctx.staged_changes) == 1
    assert "workspace_catalog" in ctx.capabilities
    summary = wc.workspace_summary_text(ctx)
    assert "src" in summary and "README.md" in summary
    assert "v3" in summary and "1 项暂存修改" in summary


def test_root_missing_status_surfaces_through_catalog(tmp_path, monkeypatch):
    created = _install_workspace(tmp_path, monkeypatch)
    wsid = created["workspace_id"]
    monkeypatch.setattr(wc, "_server_is_healthy", _healthy)
    monkeypatch.setattr(wc, "_advertised_tools", _only_catalog)
    monkeypatch.setattr(wc, "_call_electron", _fake_electron(catalog_status="WORKSPACE_ROOT_MISSING"))
    ctx = asyncio.run(wc.load_workspace_context("u1", workspace_id=wsid))
    assert ctx.status_code == wc.WORKSPACE_ROOT_MISSING
    assert ctx.available is False
    # 降级摘要不得鼓励模型假装看到文件。
    assert "不要假装看到文件" in wc.workspace_summary_text(ctx)


def test_version_cache_skips_full_catalog_when_unchanged(tmp_path, monkeypatch):
    created = _install_workspace(tmp_path, monkeypatch)
    wsid = created["workspace_id"]
    calls = {"diff": 0, "catalog": 0}

    async def _call(server_name, tool_name, args, **_kwargs):
        if tool_name == "workspace_diff":
            calls["diff"] += 1
            return {"status": "success", "data": {"base_version": 7}}
        if tool_name == "workspace_catalog":
            calls["catalog"] += 1
            return {"status": "success", "data": {"version": 7, "entries": [{"path": "a.txt", "type": "file"}]}}
        return {"status": "failed", "error_code": "WORKSPACE_READ_FAILED"}

    monkeypatch.setattr(wc, "_server_is_healthy", _healthy)
    monkeypatch.setattr(wc, "_advertised_tools", _advertised)
    monkeypatch.setattr(wc, "_call_electron", _call)
    monkeypatch.setattr(wc, "_cache_write", _no_cache_write)
    monkeypatch.setattr(wc, "_cache_read", _no_cache_read)

    ctx1 = asyncio.run(wc.load_workspace_context("u1", workspace_id=wsid))
    assert ctx1.version == 7
    assert calls["catalog"] == 1

    # 第二次：版本一致 → 直接复用缓存快照，不再触发 catalog。
    snapshot = {"ts": 0, "version": 7, "entries": [{"path": "a.txt", "type": "file"}],
                "staged_changes": [], "capabilities": []}

    async def _read_snapshot(_workspace_id: str):
        return dict(snapshot)

    monkeypatch.setattr(wc, "_cache_read", _read_snapshot)
    ctx2 = asyncio.run(wc.load_workspace_context("u1", workspace_id=wsid))
    assert ctx2.available is True
    assert calls["catalog"] == 1


def test_desktop_registry_prefers_device_specific_row(monkeypatch):
    monkeypatch.setattr(
        registry_module.settings, "MCP_SERVERS",
        [
            {"name": "lumi_shared", "url": "http://shared/mcp", "provider_type": "desktop_mcp"},
            {"name": "lumi_pc_1", "url": "http://pc1/mcp", "provider_type": "desktop_mcp",
             "user_id": "u1", "device_id": "dev1"},
            {"name": "lumi_pc_2", "url": "http://pc2/mcp", "provider_type": "desktop_mcp",
             "user_id": "u1", "device_id": "dev2"},
        ],
    )
    endpoint = registry_module.desktop_connections.resolve_desktop(user_id="u1", device_id="dev2")
    assert endpoint is not None and endpoint.name == "lumi_pc_2"
    names = registry_module.desktop_connections.desktop_server_names(user_id="u1", device_id="dev1")
    assert "lumi_pc_2" not in names
    assert "lumi_pc_1" in names


def test_workspace_reading_capabilities_only_expose_aggregated_entry(tmp_path, monkeypatch):
    created = _install_workspace(tmp_path, monkeypatch)
    wsid = created["workspace_id"]

    async def fake_list_tools(_server):
        return [
            {"name": "workspace_catalog", "description": "", "input_schema": {}, "permission": "user"},
            {"name": "workspace_list", "description": "", "input_schema": {}, "permission": "user"},
            {"name": "workspace_read", "description": "", "input_schema": {}, "permission": "user"},
            {"name": "workspace_search", "description": "", "input_schema": {}, "permission": "user"},
            {"name": "workspace_stage_write", "description": "", "input_schema": {}, "permission": "user", "write_op": True},
            {"name": "workspace_diff", "description": "", "input_schema": {}, "permission": "user"},
            {"name": "sandbox_run", "description": "", "input_schema": {}, "permission": "user"},
        ]

    monkeypatch.setattr("app.agents.mcp.manager.list_tools", fake_list_tools)
    monkeypatch.setattr("app.agents.mcp.manager.server_is_healthy", lambda _s: True)

    caps = asyncio.run(executor_module.get_workspace_navigator_capability("u1", "office", "user", wsid))
    names = {item.name for item in caps}
    # 模型只看到一个读取入口；内部原子名不再进入 function calling schema。
    assert names == {"mcp__lumi_pc__workspace_navigator"}
    capability = caps[0]
    assert capability.domain == "workspace"
    assert capability.write_op is False
    assert capability.annotations["workspace_read_domain"] is True
    assert set(capability.parameters["properties"]) == {
        "action", "path", "query", "search_path", "search_mode",
        "depth", "cursor", "max_chars", "read_to_end", "max_results", "include_ignored",
        # scan（代码骨架）：了解结构用 scan，再用 read + 行区间精读。
        "start_line", "end_line", "kind", "find", "max_symbols", "include_imports",
    }
    assert capability.parameters["required"] == ["action"]
    assert capability.parameters["properties"]["action"]["enum"] == [
        "list", "search", "read", "scan",
    ]
    assert "workspace_id" not in capability.parameters["properties"]
    ok, leaked = wc.validate_model_readonly_capabilities(
        {item.raw_name for item in caps}
    )
    assert ok and leaked == []


def test_reading_capabilities_empty_without_workspace_scope(tmp_path, monkeypatch):
    caps = asyncio.run(executor_module.get_workspace_navigator_capability("u1", "office", "user", ""))
    assert caps == []
