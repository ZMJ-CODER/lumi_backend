"""C 项回归：任意工具结果都能产出模型/前端/审计/持久化四类投影。

验收标准 #3 的落点。覆盖三个**生产出口**：

* 前端：节点完成时 ``display.ui``（分页游标、条目、来源）来自 UI 投影；
* 审计：技能调用日志里的 ``audit`` 记录来自审计投影（无正文）；
* 持久化：结果引用旁边附带最小快照，恢复时不必先拉正文。
"""

from __future__ import annotations

import asyncio
import json

from app.agents.skills.base import ToolOutput
from app.contracts.projections import (
    project_all_results,
    project_result,
    result_audit,
    result_model,
    result_storage,
    result_ui,
)

NAVIGATOR_ENVELOPE = {
    "status": "success",
    "action": "read",
    "summary": "已读取 2 个文件",
    # 分页事实在顶层（与 workspace_navigator 的生产形态一致）
    "has_more": True,
    "cursor": "v1:README.md:40",
    "data": {
        "path": "README.md",
        "sections": [
            {"source": "README.md", "location": "1-40", "title": "简介", "text": "正文内容" * 20},
        ],
        "entries": [{"path": "README.md", "kind": "file", "size": 120}],
    },
    "meta": {"total_size": 999, "workspace_version": 7},
    "content_type": "structured",
}


def test_all_four_projections_are_produced_for_a_tool_result():
    views = project_all_results(NAVIGATOR_ENVELOPE, tool_name="workspace_navigator")
    assert set(views) == {"model", "ui", "audit", "storage"}
    # 模型投影：可读分段 + 分页提示（不含内部 meta 噪音）
    assert "README.md" in views["model"]["text"]
    assert "cursor" in views["model"]["text"]
    # UI 投影：前端要的展示字段都在
    ui = views["ui"]
    assert ui["status"] == "success"
    assert ui["has_more"] is True and ui["cursor"] == "v1:README.md:40"
    assert ui["entries"][0]["path"] == "README.md"
    assert ui["sections"][0]["source"] == "README.md"
    # 审计投影：有执行记录、没有正文
    audit = views["audit"]
    assert audit["tool"] == "workspace_navigator"
    assert audit["error_code"] is None
    assert "正文内容" not in json.dumps(audit, ensure_ascii=False)
    # 持久化投影：白名单最小快照，不落正文
    storage = views["storage"]
    assert storage["has_more"] is True and storage["cursor"] == "v1:README.md:40"
    assert "正文内容" not in json.dumps(storage, ensure_ascii=False)


def test_individual_projection_helpers_handle_legacy_shapes():
    legacy = ToolOutput(
        status="failed", data="boom", error="失败", error_code="EXEC_ERROR", retryable=True
    )
    ui = result_ui(legacy, tool_name="demo_tool")
    assert ui["status"] == "failed"
    assert ui["error"]["code"] == "EXEC_ERROR"
    audit = result_audit(legacy, tool_name="demo_tool")
    assert audit["status"] == "failed" and audit["error_code"] == "EXEC_ERROR"
    assert result_storage(legacy)["status"] == "failed"
    # 第三方 MCP 旧形态也可投影
    third_party = {"success": True, "content": "done", "metadata": {}, "is_error": False}
    assert project_result(third_party, "ui")["status"] == "success"


def test_projection_failure_never_loses_the_result():
    class _Exploding:
        @property
        def payload(self):
            raise RuntimeError("boom")

        def __getattr__(self, item):
            raise RuntimeError("boom")

    # 适配失败的输入 → 空投影（绝不抛给调用方）
    assert project_result(_Exploding(), "ui") == {}
    assert project_all_results(_Exploding()) == {}
    assert result_model(_Exploding()) == ""


def test_node_display_carries_ui_projection():
    from app.agents.orchestration.execution.presentation import attach_display_result

    class _Node:
        agent = "workspace"
        name = "读文件"
        params = {"skill_name": "workspace_read"}

    value = attach_display_result(_Node(), NAVIGATOR_ENVELOPE)
    assert value["display"]["completed"]
    ui = value["display"]["ui"]
    assert ui["cursor"] == "v1:README.md:40" and ui["entries"]


def test_skill_audit_log_record_contains_contract_audit(monkeypatch):
    """技能调用审计详情里应带契约审计投影（结构化、无正文）。"""
    import app.agents.skills.executor as executor

    captured: dict = {}

    class _FakeSession:
        def add(self, row):
            captured["detail"] = row.detail
            captured["action"] = row.action

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(executor, "async_session_factory", lambda: _FakeSession())

    class _Skill:
        name = "demo_skill"

    result = ToolOutput(status="success", data={"entries": [{"path": "a.txt"}]}, content_type="structured")
    asyncio.run(executor._record_skill_log("11111111-1111-1111-1111-111111111111", _Skill(), {}, result))
    detail = json.loads(captured["detail"])
    assert detail["audit"]["tool"] == "demo_skill"
    assert detail["audit"]["status"] == "success"
    assert captured["action"] == "skill:demo_skill"


def test_persisted_result_ref_carries_a_body_free_storage_snapshot():
    from app.agents.orchestration.execution.lineage import (
        persist_result_ref,
        resolve_result_ref,
        resolve_result_storage,
    )

    async def scenario():
        ref = await persist_result_ref("u1", dict(NAVIGATOR_ENVELOPE))
        assert ref
        storage = await resolve_result_storage("u1", ref)
        body = await resolve_result_ref("u1", ref)
        return storage, body

    storage, body = asyncio.run(scenario())
    assert storage and storage["cursor"] == "v1:README.md:40"
    assert "正文内容" not in json.dumps(storage, ensure_ascii=False)
    # 正文仍可解析（恢复路径不受影响）
    assert body and body.get("data")
