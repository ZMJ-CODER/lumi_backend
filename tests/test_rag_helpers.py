"""RAG 混合检索核心逻辑测试（不依赖数据库/模型）."""

import asyncio

from app.services.rag.knowledge import (
    _chunk_metadata,
    _extract_keywords,
    _hybrid_fuse,
    _multi_query_fuse,
    _passes_similarity_gate,
    _scope_conditions,
)
import json
from app.services.rag.query_rewriter import _rewrite_local, get_retrieval_queries, should_expand_query
from app.services.rag.document_parser import ParsedDocument, ParsedSegment, parse_document_with_metadata
from app.services.rag.chunker import chunk_structured


def test_extract_keywords_chinese_phrase():
    kws = _extract_keywords("协议以支")
    assert kws, "应能提取关键词"
    assert any("协议" in k or "以支" in k for k in kws)


def test_hybrid_fuse_marks_kw_hit():
    vector_rows = [
        {"chunk_id": "a", "chunk_text": "A", "title": "t", "document_id": "d1",
         "is_public": False, "created_at": None, "category": "general", "similarity": 0.3}
    ]
    keyword_rows = [
        {"chunk_id": "a", "chunk_text": "A", "title": "t", "document_id": "d1",
         "is_public": False, "created_at": None, "category": "general", "kw_score": 2}
    ]
    fused = _hybrid_fuse(vector_rows, keyword_rows, 5)
    assert fused[0]["kw_hit"] is True
    assert fused[0]["similarity"] == 0.3


def test_hybrid_fuse_preserves_chunk_metadata_for_citations():
    rows = _hybrid_fuse(
        [{"chunk_id": "a", "chunk_text": "A", "chunk_metadata": '{"heading_path":"付款"}',
          "title": "t", "document_id": "d1", "is_public": False,
          "created_at": None, "category": "general", "similarity": 0.8}], [], 5,
    )
    assert rows[0]["chunk_metadata"] == '{"heading_path":"付款"}'


def test_multi_query_fuse_deduplicates_and_keeps_keyword_evidence():
    rows = _multi_query_fuse(
        [
            [{"chunk_id": "a", "score": 0.1, "kw_hit": False, "similarity": 0.8}],
            [{"chunk_id": "a", "score": 0.1, "kw_hit": True, "similarity": None}],
        ],
        5,
    )
    assert len(rows) == 1
    assert rows[0]["kw_hit"] is True
    assert rows[0]["similarity"] == 0.8


def test_passes_similarity_gate():
    # 关键词命中 → 无论相似度高低都放行
    assert _passes_similarity_gate({"kw_hit": True, "similarity": 0.3}, 0.7)
    # 关键词路独有（无向量相似度）→ 放行
    assert _passes_similarity_gate({"kw_hit": False, "similarity": None}, 0.7)
    # 纯向量且低于阈值 → 丢弃
    assert not _passes_similarity_gate({"kw_hit": False, "similarity": 0.5}, 0.7)
    # 纯向量且达标 → 放行
    assert _passes_similarity_gate({"kw_hit": False, "similarity": 0.8}, 0.7)


def test_scope_conditions_own_space_override():
    uid = "11111111-1111-1111-1111-111111111111"
    conds_override, _ = _scope_conditions(uid, ["chat"], need_embedding=False, own_space_override=True)
    assert any("s.is_public = false OR" in c for c in conds_override)
    conds_exact, _ = _scope_conditions(uid, ["chat"], need_embedding=False, own_space_override=False)
    assert any(c.strip() == "s.scene_tag IN :tags" for c in conds_exact)


def test_scope_conditions_excludes_code():
    conds, params = _scope_conditions(None, [], need_embedding=False, exclude_categories=["code"])
    assert any("NOT IN" in c for c in conds)
    assert params["excl_cats"] == ["code"]


def test_local_query_rewriter_rejects_vision_model(monkeypatch):
    from app.services.rag import query_rewriter

    monkeypatch.setattr(query_rewriter.settings, "RAG_QUERY_REWRITE_MODEL", "qwen2.5vl:7b")
    assert asyncio.run(_rewrite_local("查找合同")) is None


def test_query_expansion_keeps_original_and_skips_exact_literals(monkeypatch):
    from app.services.rag import query_rewriter

    async def fake_rewrite(text, user_id=None):  # noqa: ARG001
        return "合同逾期付款违约金计算条款"

    monkeypatch.setattr(query_rewriter, "rewrite_query", fake_rewrite)
    queries = asyncio.run(get_retrieval_queries("合同逾期违约金怎么算", scene="office"))
    assert queries == ["合同逾期违约金怎么算", "合同逾期付款违约金计算条款"]
    assert not should_expand_query("请查 A-2024-001.pdf", scene="office")


def test_structured_chunker_repeats_heading_path_and_table_header():
    text = "# 合同\n## 付款条款\n" + "正文。" * 300 + "\n| 项目 | 金额 |\n| --- | --- |\n| A | 1 |\n"
    chunks = chunk_structured(text, chunk_size=120, overlap=20)
    assert len(chunks) > 1
    assert all("# 合同\n## 付款条款" in chunk for chunk in chunks)
    assert any("| 项目 | 金额 |" in chunk for chunk in chunks)


def test_chunk_metadata_keeps_verifiable_structural_locator():
    raw = _chunk_metadata("handbook.md", "# 付款条款\n\n付款日期为月底。", 3)
    locator = json.loads(raw)
    assert locator["chunk_index"] == 3
    assert locator["heading_path"] == "付款条款"
    assert locator["source"] == "handbook.md"


def test_chunk_metadata_carries_only_supplied_page_locator():
    raw = _chunk_metadata("contract.pdf", "付款条款", 0, {"page_start": 3, "page_end": 4})
    locator = json.loads(raw)
    assert locator["page_start"] == 3
    assert locator["page_end"] == 4
    assert "table_id" not in locator


def test_structured_parser_contract_is_explicit():
    parsed = ParsedDocument("正文", [ParsedSegment("正文", page_start=2, page_end=2)], "pypdf")
    assert parsed.segments[0].page_start == 2
    assert parsed.parser == "pypdf"


def test_pdf_parser_contract_keeps_page_segments(monkeypatch, tmp_path):
    from app.services.rag import document_parser

    source = tmp_path / "contract.pdf"
    source.write_bytes(b"placeholder")
    monkeypatch.setattr(
        document_parser,
        "_parse_pdf_segments",
        lambda _: [ParsedSegment("page one", page_start=1, page_end=1)],
    )
    parsed = parse_document_with_metadata(str(source), "contract.pdf")
    assert parsed.parser == "pypdf"
    assert parsed.segments[0].page_start == 1


def test_sparse_experiment_is_disabled_by_default(monkeypatch):
    from app.services.rag import sparse_embeddings

    monkeypatch.setattr(sparse_embeddings.settings, "RAG_SPARSE_EXPERIMENT_ENABLED", False)
    assert asyncio.run(sparse_embeddings.embed_sparse_texts(["合同条款"])) == []


def test_reranker_is_a_noop_when_feature_is_disabled(monkeypatch):
    from app.services.rag import reranker

    monkeypatch.setattr(reranker.settings, "RAG_RERANK_ENABLED", False)
    rows = [{"chunk_text": "A"}, {"chunk_text": "B"}]
    assert reranker.rerank("q", rows, 1) == rows[:1]
