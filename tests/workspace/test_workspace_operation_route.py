"""executor ↔ 统一操作契约的接线回归（真跑 ``execute_tool_call``）。

验证三件事：

1. 四个操作工具（``workspace.write/edit/move/delete``）在 executor 里被操作网关接管，
   结果折进既有 ``SkillResult``（``data`` 就是 ``OperationResult``）；
2. **审批没有被绕过**：需要审批的能力在没有确切审批指纹时返回 ``pending_approval`` +
   ``APPROVAL_REQUIRED``，不会去调客户端；
3. 带上确切指纹后真的执行，并把 ``operation_summary``（供 Job/RunView 恢复）带进 metadata。
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.skills import executor as ex

WORKSPACE = "ws-ops"
USER = "u1"
CONV = "conv-ops"


@pytest.fixture(autouse=True)
def _available_effect_journal(monkeypatch):
    """隔离副作用安全日志（与其余写类用例一致，防跨文件污染）。"""
    from app.agents.orchestration.runtime import effects
    from app.repositories.effect_journal_repository import InMemoryEffectJournalRepository

    monkeypatch.setattr(effects, "_repository", InMemoryEffectJournalRepository())


@pytest.fixture(autouse=True)
def _operation_tools_registered():
    """确保内部操作工具已注册（插件在应用启动时装载，单测里要显式装一次）。"""
    from app.agents.skills.loader import load_skill_plugins
    from app.agents.skills.registry import ToolRegistry

    if ToolRegistry.get("workspace_edit") is None:
        load_skill_plugins()
    yield


def _client(monkeypatch, files: dict[str, str] | None = None):
    """把网关的默认客户端换成内存实现（不触碰真实 MCP）。"""
    from app.agents.skills import workspace_operation_route as route
    # 跨测试文件复用 FakeClient：P7 镜像后必须写全路径（裸名只对 tests/ 根下的模块有效）
    from tests.workspace.test_workspace_operations import FakeClient

    fake = FakeClient(files or {})
    monkeypatch.setattr(route, "_default_client", lambda **kwargs: fake)
    return fake


def _call(name: str, arguments: dict, **kwargs):
    return asyncio.run(
        ex.execute_tool_call(
            {"id": "call-1", "type": "function", "function": {"name": name, "arguments": arguments}},
            USER,
            "office",
            CONV,
            authorized_workspace_id=WORKSPACE,
            allow_internal=True,
            **kwargs,
        )
    )


def _fingerprint(name: str, args: dict) -> frozenset[str]:
    return frozenset({ex.tool_call_fingerprint(name, args)})


def test_edit_without_approval_returns_pending_approval(monkeypatch):
    """需要审批的能力没有审批指纹 → 待审批（不是执行、也不是普通失败）。"""
    from app.workspace.write.revision import revision_for_text

    _client(monkeypatch, {"a.py": "old\n"})
    args = {
        "path": "a.py",
        "old_str": "old",
        "new_str": "new",
        "expected_revision": revision_for_text("old\n"),
    }
    result = _call("workspace_edit", args)
    assert result.status == "pending_approval"
    assert result.error_code == "APPROVAL_REQUIRED"
    assert result.metadata["operation"] == "edit"
    assert result.metadata["approval_state"] == "pending"
    assert result.retryable is True


def test_edit_with_fingerprint_executes_through_operation_gateway(monkeypatch):
    """带上确切审批指纹 → 真的走操作网关（版本校验 + 读回 + OperationResult）。"""
    from app.workspace.write.revision import revision_for_text

    client = _client(monkeypatch, {"a.py": "old\n"})
    args = {
        "path": "a.py",
        "old_str": "old",
        "new_str": "new",
        "expected_revision": revision_for_text("old\n"),
    }
    result = _call("workspace_edit", args, confirmed_tool_calls=_fingerprint("workspace_edit", args))
    assert result.success is True
    assert client.files["a.py"] == "new\n"
    assert result.data["operation"] == "edit"
    assert result.data["status"] == "success"
    assert result.data["new_revision"] == revision_for_text("new\n")
    summary = result.metadata["operation_summary"]
    assert summary["operation"] == "edit"
    assert summary["logical_path"] == "a.py"
    assert summary["changed_files"] == ["a.py"]
    assert summary["rollback_available"] is True


def test_mismatched_fingerprint_does_not_authorize(monkeypatch):
    """批准的是 A、实际调 B：指纹不符 → 仍然待审批（不得靠工具名蒙混）。"""
    from app.workspace.write.revision import revision_for_text

    client = _client(monkeypatch, {"a.py": "old\n"})
    approved_args = {
        "path": "a.py",
        "old_str": "old",
        "new_str": "new",
        "expected_revision": revision_for_text("old\n"),
    }
    tampered = dict(approved_args, new_str="TAMPERED")
    result = _call(
        "workspace_edit", tampered, confirmed_tool_calls=_fingerprint("workspace_edit", approved_args)
    )
    assert result.status == "pending_approval"
    assert result.error_code == "APPROVAL_REQUIRED"
    assert client.files["a.py"] == "old\n", "未审批不得写入"


def test_delete_requires_approval_then_reports_trash_entry(monkeypatch):
    """删除工具：审批后默认进回收站，结果里带 entry_id 与可恢复标记。"""
    client = _client(monkeypatch, {"docs/a.md": "hello\n"})
    args = {"path": "docs/a.md"}
    pending = _call("workspace_delete", args)
    assert pending.status == "pending_approval"

    done = _call(
        "workspace_delete", args, confirmed_tool_calls=_fingerprint("workspace_delete", args)
    )
    assert done.success is True
    assert "docs/a.md" not in client.files
    assert done.data["stats"]["to_trash"] is True
    assert done.data["stats"]["entry_id"]
    assert done.data["rollback_available"] is True


def test_operation_route_without_workspace_scope_is_rejected(monkeypatch):
    """没有绑定工作区 → 结构化拒绝（不允许在未知范围里操作）。"""
    _client(monkeypatch)
    result = asyncio.run(
        ex.execute_tool_call(
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "workspace_edit", "arguments": {"path": "a.py", "old_str": "a", "new_str": "b"}},
            },
            USER,
            "office",
            CONV,
            authorized_workspace_id="",
            allow_internal=True,
        )
    )
    assert result.success is False
    assert result.error_code == "WORKSPACE_SCOPE_REQUIRED"


def test_non_operation_tools_are_not_intercepted(monkeypatch):
    """非操作工具不经过这一层（返回 None → 旧路径）。"""
    from app.agents.skills.workspace_operation_route import try_workspace_operation

    outcome = asyncio.run(
        try_workspace_operation(
            tool_name="workspace_read",
            args={"path": "a.py"},
            user_id=USER,
            conversation_id=CONV,
            workspace_id=WORKSPACE,
        )
    )
    assert outcome is None
