"""Deterministic first-pass targeting for questions over a document set."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.agents.core.base import WorkerAgent, WorkerContext
from app.agents.core.progress import set_progress

if TYPE_CHECKING:
    from app.agents.orchestration.models import TaskNode


_LATIN_TOKEN = re.compile(r"[a-z0-9]{3,}", re.IGNORECASE)
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
_GENERIC = frozenset({"帮我", "一下", "这些", "文件", "附件", "资料", "找出", "哪份", "哪个", "是否", "有没有", "里面", "请问", "看看"})


def _query_terms(query: str) -> set[str]:
    """Return bounded lexical signals; this is deliberately not an LLM ranker."""
    text = str(query or "").lower()
    terms = set(_LATIN_TOKEN.findall(text))
    for run in _CJK_RUN.findall(text):
        # CJK has no whitespace. Small overlapping phrases allow a summary
        # containing "付款期限" to match a longer natural-language question.
        for width in (2, 3, 4):
            terms.update(run[index:index + width] for index in range(max(0, len(run) - width + 1)))
    return {term for term in terms if term not in _GENERIC}


def rank_document_cards(query: str, cards: list[dict]) -> list[tuple[int, dict]]:
    """Rank summaries by exact lexical evidence without reading document bodies."""
    terms = _query_terms(query)
    ranked: list[tuple[int, dict]] = []
    for card in cards:
        haystack = (str(card.get("filename") or "") + "\n" + str(card.get("summary") or "")).lower()
        score = sum(1 for term in terms if term in haystack)
        ranked.append((score, card))
    return sorted(ranked, key=lambda item: (-item[0], str(item[1].get("doc_id") or "")))


def choose_unique_document(query: str, cards: list[dict]) -> tuple[dict | None, list[dict], dict]:
    """Select only when the compact overview gives a clear single winner."""
    ranked = rank_document_cards(query, cards)
    candidates = [
        {"doc_id": str(card.get("doc_id") or ""), "score": score}
        for score, card in ranked[:5]
    ]
    if not ranked or ranked[0][0] <= 0:
        return None, candidates, {"selection_confidence": "low", "reason": "no_summary_match"}
    best_score, best = ranked[0]
    next_score = ranked[1][0] if len(ranked) > 1 else 0
    # A close second candidate is a genuine selection ambiguity, not a reason
    # to manufacture certainty.  For a clear winner with a weak-but-relevant
    # second summary, we retain the fixed path but ask the agent to perform a
    # bounded coverage read below.  This prevents a single-document answer
    # from silently excluding a partly relevant attachment.
    close_second = next_score > 0 and next_score * 2 >= best_score
    unique = (
        (best_score >= 2 and best_score > next_score and not close_second)
        or (len(ranked) == 1 and best_score >= 1)
    )
    if not unique:
        return None, candidates, {"selection_confidence": "ambiguous", "reason": "summary_scores_overlap"}
    secondary = ranked[1][1] if len(ranked) > 1 and next_score > 0 else None
    return best, candidates, {
        "selection_confidence": "high",
        "reason": "unique_summary_match",
        "coverage_check_required": secondary is not None,
        "coverage_doc_id": str((secondary or {}).get("doc_id") or ""),
    }


class DocumentTargetingAgent(WorkerAgent):
    """Inspect → deterministically choose → scoped read, without ReAct."""

    name = "document_targeting"
    description = "多文档事实定位：盘点摘要后只读取唯一高置信候选文档"
    params_help = '{"query":"要定位的事实", "office_docs":[{"doc_id":"..."}]}'
    skills = ["inspect_document_set", "read_document"]

    async def execute(self, node: TaskNode, ctx: WorkerContext) -> dict:
        query = str(node.params.get("query") or node.params.get("instruction") or "").strip()
        if not query or len(ctx.office_doc_ids) < 2:
            return {
                "success": False,
                "error": "多文档定位需要至少两份已授权文档和查询问题",
                "error_code": "INVALID_ARGS",
            }
        await set_progress(ctx.job_id, node.id, "正在盘点已授权文档…")
        overview = await self.run_skill(
            "inspect_document_set", {"scope": "office_docs", "query": query}, ctx
        )
        if not overview.get("success"):
            return overview
        # ``run_skill`` flattens SkillResult.metadata into its result object.
        # Custom test Skills sometimes retain it under metadata, hence both
        # forms are accepted without widening the document authorization set.
        cards = overview.get("documents")
        if not isinstance(cards, list):
            nested = overview.get("metadata")
            cards = nested.get("documents") if isinstance(nested, dict) else []
        selected, candidates, confidence = choose_unique_document(query, cards or [])
        selection = {
            "query": query[:500],
            "candidate_documents": candidates,
            **confidence,
            "strategy": "inspect_then_scoped_read",
        }
        if selected is None:
            return {
                "success": False,
                "error": "文档摘要无法唯一定位目标，已升级为受限动态核验。",
                "error_code": "DOCUMENT_SELECTION_AMBIGUOUS",
                "retryable": False,
                "tool_metadata": {"document_selection": selection},
            }
        doc_id = str(selected.get("doc_id") or "")
        await set_progress(ctx.job_id, node.id, "已定位候选文档，正在读取…")
        read = await self.run_skill("read_document", {"doc_id": doc_id}, ctx)
        if not read.get("success"):
            return read
        read_selection = read.get("document_selection") if isinstance(read, dict) else {}
        selection.update({
            "selected_doc_id": doc_id,
            "selected_filename": str(selected.get("filename") or ""),
            "read_confirmed": True,
        })
        if isinstance(read_selection, dict):
            selection.update({key: value for key, value in read_selection.items() if value})
        content = str(read.get("content") or "")
        coverage_doc_id = str(confidence.get("coverage_doc_id") or "")
        if confidence.get("coverage_check_required") and coverage_doc_id:
            await set_progress(ctx.job_id, node.id, "正在核验另一份相关文档…")
            coverage = await self.run_skill("read_document", {"doc_id": coverage_doc_id}, ctx)
            if not coverage.get("success"):
                return {
                    "success": False,
                    "error": "另一份相关文档无法完成核验，已升级为受限动态核验。",
                    "error_code": "DOCUMENT_SELECTION_AMBIGUOUS",
                    "retryable": False,
                    "tool_metadata": {"document_selection": selection},
                }
            coverage_card = next(
                (card for card in cards or [] if str(card.get("doc_id") or "") == coverage_doc_id),
                {},
            )
            content += (
                "\n\n--- 覆盖核验文档："
                + str(coverage_card.get("filename") or coverage_doc_id)
                + " ---\n"
                + str(coverage.get("content") or "")
            )
            selection["coverage_checked_doc_ids"] = [doc_id, coverage_doc_id]
            selection["coverage_check_reason"] = "excluded_summary_partially_related"
        return {
            "success": True,
            "content": content,
            "output": content,
            "doc_id": doc_id,
            "filename": str(selected.get("filename") or ""),
            "tool_metadata": {"document_selection": selection},
            "step_title": "定位并读取相关文档",
        }
