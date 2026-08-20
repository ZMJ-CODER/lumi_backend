"""RAG 混合检索核心逻辑测试（不依赖数据库/模型）."""

import asyncio

from app.services.rag.knowledge import (
    _extract_keywords,
    _hybrid_fuse,
    _passes_similarity_gate,
    _scope_conditions,
)
from app.services.rag.query_rewriter import _rewrite_local


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
