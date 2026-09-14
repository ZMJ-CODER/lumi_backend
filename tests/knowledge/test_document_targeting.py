"""Regression coverage for deterministic multi-document targeting."""

import asyncio

from app.agents.core.base import WorkerContext
from app.agents.orchestration.models import TaskNode
from app.agents.roles.knowledge.document_targeting import (
    DocumentTargetingAgent,
    choose_unique_document,
)


def test_summary_ranker_selects_only_a_clear_single_candidate():
    selected, candidates, confidence = choose_unique_document(
        "哪份文件写了付款期限和违约条款",
        [
            {"doc_id": "a", "filename": "报价.pdf", "summary": "产品报价和交付范围"},
            {"doc_id": "b", "filename": "合同.pdf", "summary": "付款期限、违约条款和争议解决"},
        ],
    )
    assert selected and selected["doc_id"] == "b"
    assert candidates[0]["doc_id"] == "b"
    assert confidence["selection_confidence"] == "high"


def test_summary_ranker_refuses_near_tie_for_react_fallback():
    selected, _, confidence = choose_unique_document(
        "付款期限",
        [
            {"doc_id": "a", "filename": "合同一.pdf", "summary": "付款期限为 30 天"},
            {"doc_id": "b", "filename": "合同二.pdf", "summary": "付款期限为 45 天"},
        ],
    )
    assert selected is None
    assert confidence["selection_confidence"] == "ambiguous"


def test_targeting_agent_reads_only_the_selected_authorized_document(monkeypatch):
    agent = DocumentTargetingAgent()
    calls: list[tuple[str, dict]] = []

    async def fake_skill(name, params, _ctx):
        calls.append((name, params))
        if name == "inspect_document_set":
            return {
                "success": True,
                "documents": [
                    {"doc_id": "a", "filename": "报价.pdf", "summary": "产品报价"},
                    {"doc_id": "b", "filename": "合同.pdf", "summary": "付款期限和违约条款"},
                ],
            }
        return {"success": True, "content": "合同正文", "document_selection": {"selected_doc_id": "b"}}

    async def no_progress(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agent, "run_skill", fake_skill)
    monkeypatch.setattr("app.agents.roles.knowledge.document_targeting.set_progress", no_progress)
    result = asyncio.run(agent.execute(
        TaskNode(id="n1", agent="document_targeting", params={"query": "哪份文件有付款期限和违约条款"}),
        WorkerContext(user_id="u1", job_id="j1", office_doc_ids=("a", "b")),
    ))
    assert result["success"] is True
    assert calls == [
        ("inspect_document_set", {"scope": "office_docs", "query": "哪份文件有付款期限和违约条款"}),
        ("read_document", {"doc_id": "b"}),
    ]
    assert result["tool_metadata"]["document_selection"]["selected_doc_id"] == "b"


def test_targeting_agent_performs_bounded_coverage_read_for_weak_related_second_document(monkeypatch):
    agent = DocumentTargetingAgent()
    calls: list[tuple[str, dict]] = []

    async def fake_skill(name, params, _ctx):
        calls.append((name, params))
        if name == "inspect_document_set":
            return {
                "success": True,
                "documents": [
                    {"doc_id": "a", "filename": "terms-summary.pdf", "summary": "payment delivery summary"},
                    {"doc_id": "b", "filename": "master-agreement.pdf", "summary": "payment breach liability delivery termination"},
                ],
            }
        return {"success": True, "content": f"正文-{params['doc_id']}"}

    async def no_progress(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agent, "run_skill", fake_skill)
    monkeypatch.setattr("app.agents.roles.knowledge.document_targeting.set_progress", no_progress)
    result = asyncio.run(agent.execute(
        TaskNode(id="n2", agent="document_targeting", params={"query": "payment breach liability delivery termination"}),
        WorkerContext(user_id="u1", job_id="j1", office_doc_ids=("a", "b")),
    ))
    assert result["success"] is True
    assert calls[-2:] == [("read_document", {"doc_id": "b"}), ("read_document", {"doc_id": "a"})]
    selection = result["tool_metadata"]["document_selection"]
    assert selection["coverage_checked_doc_ids"] == ["b", "a"]
    assert "正文-a" in result["content"]
