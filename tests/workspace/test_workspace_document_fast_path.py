"""Regression coverage for trusted workspace documents and low-cost plans."""

from __future__ import annotations

import asyncio
import json

from app.agents.orchestration.execution.node import ApplicationTaskNodeExecutor
from app.agents.orchestration.models import Job, TaskNode
from app.agents.orchestration.planning.office_plan_selection_service import OfficePlanSelectionService
from app.agents.orchestration.planning.context import PlanRequestContext
from app.agents.orchestration.planning.contracts import TaskTree
from app.agents.orchestration.submission.submission_context_service import SubmissionContextService
from app.agents.orchestration.planning.tca import ComplexityLevel, TaskComplexityAssessor
from app.workspace import service as workspaces


class _NeverPlanner:
    async def plan_for_level(self, *_args, **_kwargs):  # pragma: no cover - assertion is the point
        raise AssertionError("M0/M1 fast path must not invoke the planner")


def _context(request: str, docs: list[dict]) -> PlanRequestContext:
    return PlanRequestContext.from_legacy_args(
        "u1", request, "office", None, None, office_docs=docs
    )


def test_single_document_question_uses_minimal_read_then_answer_without_planner():
    docs = [{"doc_id": "d1", "filename": "合同.docx", "kind": "docx"}]
    service = OfficePlanSelectionService(
        planner=_NeverPlanner(), workers={}, assessor=TaskComplexityAssessor()
    )

    selection = asyncio.run(service.select(
        user_id="u1",
        request="这份合同的付款期限是什么？",
        user_role="user",
        project_id=None,
        project_ids=None,
        clarification_answer=None,
        office_docs=docs,
        prior_summaries="",
        planning_context=_context("这份合同的付款期限是什么？", docs),
        routing_model={},
    ))

    assert selection.level == ComplexityLevel.M1
    assert selection.routing["planner_invoked"] is False
    assert [node.agent for node in selection.tree.nodes] == ["atomic_step", "direct_llm"]
    assert selection.tree.nodes[0].params["preferred_tool"] == "read_document"
    assert selection.tree.nodes[0].params["inputs"] == {"doc_id": "d1"}
    assert selection.tree.nodes[1].depends_on == ["read_input"]


def test_m0_text_request_is_direct_without_planner():
    service = OfficePlanSelectionService(
        planner=_NeverPlanner(), workers={}, assessor=TaskComplexityAssessor()
    )
    selection = asyncio.run(service.select(
        user_id="u1", request="帮我把这句话改得更礼貌", user_role="user",
        project_id=None, project_ids=None, clarification_answer=None,
        office_docs=[], prior_summaries="",
        planning_context=_context("帮我把这句话改得更礼貌", []), routing_model={},
    ))
    assert selection.level == ComplexityLevel.M0
    assert selection.routing["planner_invoked"] is False
    assert selection.tree.nodes[0].agent == "direct_llm"


class _EmptyPlanner:
    supports_context_planning = True

    async def plan_for_level(self, *_args, **_kwargs):
        return TaskTree(nodes=[], error="empty", error_code="PLANNER_EMPTY")


def test_planner_empty_with_document_falls_back_to_minimal_read_plan():
    docs = [{"doc_id": "d1", "filename": "合同.docx", "kind": "docx"}]
    service = OfficePlanSelectionService(
        planner=_EmptyPlanner(), workers={}, assessor=TaskComplexityAssessor()
    )
    selection = asyncio.run(service.select(
        user_id="u1", request="先根据附件说明有哪些风险，然后再给建议", user_role="user",
        project_id=None, project_ids=None, clarification_answer=None,
        office_docs=docs, prior_summaries="", planning_context=_context("先根据附件说明有哪些风险，然后再给建议", docs),
        routing_model={},
    ))
    assert selection.routing["planner_empty"] is True
    assert selection.routing["fallback_action"] == "planning_empty_document_fallback"
    assert selection.tree.error is None
    assert selection.tree.nodes[0].params["preferred_tool"] == "read_document"


def test_workspace_registration_is_metadata_only_without_file_mirror(tmp_path, monkeypatch):
    """Workspace creation stores no staging copies, no files and no document_refs."""
    monkeypatch.setattr(workspaces, "ROOT", tmp_path / "workspaces")
    created = workspaces.create_workspace("u1", "项目", "conv-1")
    wid = created["workspace_id"]
    root = tmp_path / "workspaces" / "u1" / wid

    assert (root / "manifest.json").is_file()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["conversation_id"] == "conv-1"
    assert manifest["workspace_id"] == wid
    # No server-side file mirror, staging tree or document-ref registry.
    assert not (root / "staging").exists()
    assert not (root / "snapshots").exists()
    assert "document_refs" not in manifest
    assert list(workspaces.list_workspaces("u1"))[0]["workspace_id"] == wid

    # Ownership isolation: another user cannot resolve this conversation.
    assert workspaces.workspace_for_conversation("u2", "conv-1") is None
    assert workspaces.workspace_for_conversation("u1", "conv-1")["workspace_id"] == wid

    # ensure_workspace / get_workspace enforce user ownership.
    workspaces.ensure_workspace("u1", wid)
    try:
        workspaces.ensure_workspace("u2", wid)
    except LookupError:
        pass
    else:  # pragma: no cover
        raise AssertionError("workspace must stay private to its owner")
    try:
        workspaces.get_workspace("u2", wid)
    except LookupError:
        pass
    else:  # pragma: no cover
        raise AssertionError("workspace metadata must stay private to its owner")

    workspaces.delete_workspace("u1", wid)
    assert not root.exists()


def test_workspace_service_exposes_no_server_side_file_or_document_ref_api():
    """The removed server-file model must not resurface as public API."""
    for name in (
        "write_file", "read_file", "create_directory", "delete_path", "tree",
        "register_document_ref", "document_ref", "workspace_document_refs",
        "remove_document_refs", "parse_document_reference",
        "snapshot", "rollback", "commit",
    ):
        assert not hasattr(workspaces, name), f"removed server-file API {name} still exists"


def test_workspace_is_resolved_and_enforced_one_to_one_by_conversation(tmp_path, monkeypatch):
    monkeypatch.setattr(workspaces, "ROOT", tmp_path / "workspaces")
    first = workspaces.create_workspace("u1", "项目甲", "conv-1")
    assert workspaces.workspace_for_conversation("u1", "conv-1")["workspace_id"] == first["workspace_id"]
    second = workspaces.create_workspace("u1", "旧项目")
    try:
        workspaces.bind_workspace_to_conversation("u1", second["workspace_id"], "conv-1")
    except ValueError as exc:
        assert "已经绑定" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("one conversation must not bind two workspaces")


def test_worker_context_document_scope_comes_from_routing_and_cannot_expand():
    job = Job(
        job_id="j1", user_id="u1", request="读取合同", nodes=[],
        routing={"input_refs": [{"doc_id": "allowed", "filename": "合同.txt", "kind": "text"}]},
    )
    executor = ApplicationTaskNodeExecutor(
        job=job, workers={}, review=None, store=object(), llm_api_key=None, llm_config=None,
    )
    node = TaskNode(
        id="read", agent="atomic_step",
        params={"instruction": "读", "preferred_tool": "read_document", "inputs": {"doc_id": "allowed"}},
    )
    assert executor._authorized_office_doc_ids(node) == ("allowed",)
    node.params["inputs"]["doc_id"] = "foreign"
    assert executor._authorized_office_doc_ids(node) == ()


def test_submission_context_does_not_merge_server_side_workspace_documents(monkeypatch):
    """Workspace documents are client-supplied (Electron MCP), not server mirrors."""

    class _Memory:
        async def verify_documents(self, _user, _request, docs):
            return docs

        async def load_recall_context(self, *_args): return ""
        async def load_presentation_preferences(self, *_args): return ""

    async def fake_config(**_kwargs):
        class _Config:
            api_key = None
            def as_dict(self): return {}
        return _Config()

    monkeypatch.setattr(
        "app.agents.orchestration.submission.submission_context_service.resolve_effective_llm_config", fake_config,
    )
    service = SubmissionContextService(memory=_Memory())
    prepared = asyncio.run(service.prepare(
        user_id="u1", request="读取项目文件", scene="office", conversation_id=None,
        project_id=None, project_ids=None, request_api_key=None, clarification_answer=None,
        office_docs=[{"doc_id": "chat-doc", "filename": "临时.txt", "kind": "text"}], workspace_id="ws1",
    ))
    assert [item["doc_id"] for item in prepared.office_docs] == ["chat-doc"]
