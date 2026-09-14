"""全局补丁泄漏守卫。

背景：``tests/acceptance/workspace_navigator_sanitize._install_fake_electron``
曾经**直接赋值**把假 Electron 装到应用模块上，pytest 的 monkeypatch 撤销的只是
"假实现"这个值，于是假实现永久留在 ``app.agents.mcp.manager`` /
``app.workspace.context`` / ``app.workspace.read.reader`` 上。
后果是后续用例里 ``WorkspaceReader`` 读到了假 Electron 的内容，
``test_workspace_read_window`` 在批次里失败、单独跑却通过。

这个用例故意排在 ``test_workspace_read_handoff`` 之后（文件名排序靠后），一旦
再出现同类泄漏就会直接失败并指出是哪个模块。
"""

from __future__ import annotations

import asyncio

import pytest


def test_fake_electron_patch_does_not_leak_into_app_modules():
    import app.agents.mcp.manager as manager
    import app.workspace.context as wc
    import app.workspace.read.reader as wr

    for module, name in (
        (manager, "call_tool"),
        (manager, "list_tools"),
        (wc, "resolve_workspace_desktop"),
    ):
        assert getattr(module, name).__module__.startswith("app."), (
            f"{module.__name__}.{name} 被测试补丁污染："
            f"{getattr(module, name).__module__}"
        )
    assert wr.WorkspaceReader._route.__module__.startswith("app.")


def test_workspace_reader_is_not_served_by_fake_electron():
    """真实 reader 在没有工作区时必须是失败态，而不是拿到假内容。"""
    from app.workspace.read.reader import WorkspaceReader

    reader = WorkspaceReader(user_id="u1", workspace_id="ws1", conversation_id="c1")
    payload = asyncio.run(reader.read("README 里讲了什么"))
    assert payload["status"] == "failed"
    assert not payload.get("content")
    assert (payload.get("meta") or {}).get("error_code")


def test_installer_requires_monkeypatch_for_cleaned_state():
    """接口本身的约定：传了 monkeypatch 才负责还原。"""
    import inspect

    from tests.acceptance.workspace_navigator_sanitize import _install_fake_electron

    params = inspect.signature(_install_fake_electron).parameters
    assert "monkeypatch" in params, "安装器必须支持 pytest monkeypatch（否则补丁会泄漏）"
    assert params["monkeypatch"].default is None
    with pytest.raises(TypeError):
        _install_fake_electron()  # 缺少 calls 参数
