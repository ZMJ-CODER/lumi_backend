"""长期记忆检索：默认纯向量，关键词仅作为受控兜底.

记忆条目短、结构稳定，且错误注入的代价高于漏召回。它与文档知识库
共享 pgvector/embedding 基础设施，但不共享关键词 RRF 策略。
"""

import math
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.rag.embeddings import embed_query

_STOPWORDS = {"什么", "怎么", "如何", "为什么", "一个", "这个", "那个", "一下", "the", "and", "for", "with"}
_RRF_K = 60


def _vector_str(vec: list[float]) -> str:
    """pgvector 字符串字面量: [0.1,0.2,...]."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _to_uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _extract_keywords(query: str, top: int = 5) -> list[str]:
    """提取查询关键字：jieba 中文分词为主，拉丁词 / 中文二元组兜底（与 RAG 一致）."""
    keywords: list[str] = []
    try:
        import jieba.analyse

        tags = jieba.analyse.extract_tags(query, topK=top, withWeight=False)
        keywords.extend(t.strip() for t in tags if t.strip() and len(t.strip()) > 1)
    except Exception:  # noqa: BLE001
        pass

    seen = {k.lower() for k in keywords}
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_]{1,}", query):
        tl = token.lower()
        if tl not in seen and tl not in _STOPWORDS:
            keywords.append(token)
            seen.add(tl)
    if not keywords:
        for seg in re.findall(r"[\u4e00-\u9fff]{2,}", query):
            for i in range(len(seg) - 1):
                bigram = seg[i : i + 2]
                if bigram not in seen:
                    keywords.append(bigram)
                    seen.add(bigram)
                if len(keywords) >= top:
                    break
            if len(keywords) >= top:
                break
    return keywords[:top]


def _recency_factor(created_at, half_life_days: int | None) -> float:
    """时效因子：有半衰期的类型按指数衰减，identity 恒为 1."""
    if not half_life_days or created_at is None:
        return 1.0
    try:
        age_days = (datetime.now(timezone.utc) - created_at).total_seconds() / 86400
    except TypeError:
        return 1.0
    if age_days <= 0:
        return 1.0
    return math.exp(-age_days / half_life_days)


def _rrf_fuse(vector_rows: list[dict], keyword_rows: list[dict], top_k: int) -> list[dict]:
    """Reciprocal Rank Fusion：融合向量与关键词两路召回（与 RAG 同思路）."""
    merged: dict[str, dict] = {}
    for rank, row in enumerate(vector_rows, 1):
        mid = str(row["memory_id"])
        entry = merged.setdefault(mid, dict(row))
        entry["score"] = entry.get("score", 0.0) + 1.0 / (_RRF_K + rank)
    for rank, row in enumerate(keyword_rows, 1):
        mid = str(row["memory_id"])
        entry = merged.get(mid)
        if entry:
            entry["score"] += 1.0 / (_RRF_K + rank)
    return sorted(merged.values(), key=lambda r: r.get("score") or 0.0, reverse=True)[:top_k]


async def search_user_memories(
    session: AsyncSession,
    user_id: str,
    query: str,
    top_k: int | None = None,
) -> list[dict]:
    """混合检索用户活跃记忆。返回按相关性排序的记忆列表（不含密文，L1 只含占位符）."""
    top_k = top_k or settings.MEMORY_FACT_TOP_K
    if not query or not query.strip():
        return []
    uid = _to_uuid(user_id)
    if uid is None:
        return []

    base_conds = (
        "m.user_id = :uid AND m.is_deleted = false "
        "AND (m.expire_at IS NULL OR m.expire_at > now())"
    )
    fused: list[dict] = []

    # ── 第一路：向量相似度 top-N ──
    vec = await embed_query(query)
    if vec:
        sql = f"""
            SELECT m.id AS memory_id, m.fact, m.memory_type, m.privacy_level,
                   m.importance, m.confidence, m.created_at,
                   1 - (m.embedding <=> CAST(:qvec AS vector)) AS similarity
            FROM memories m
            WHERE {base_conds} AND m.embedding IS NOT NULL
            ORDER BY m.embedding <=> CAST(:qvec AS vector)
            LIMIT :top_k
        """
        rows = (
            await session.execute(
                text(sql).bindparams(
                    bindparam("qvec", _vector_str(vec)),
                    bindparam("top_k", settings.MEMORY_HYBRID_VECTOR_TOP_K),
                    bindparam("uid", uid),
                )
            )
        ).mappings().all()
        fused = [dict(r) for r in rows]

    # ── 可选关键词兜底（默认关闭，需先由评测集验证）──
    keywords = _extract_keywords(query)
    if settings.MEMORY_FACT_KEYWORD_FALLBACK_ENABLED and keywords:
        search_expr = "(COALESCE(m.fact, '') || COALESCE(m.fact_indexable, ''))"
        kw_conds = "(" + " OR ".join(f"{search_expr} ILIKE :kw{i}" for i in range(len(keywords))) + ")"
        match_expr = " + ".join(
            f"(CASE WHEN {search_expr} ILIKE :kw{i} THEN 1 ELSE 0 END)" for i in range(len(keywords))
        )
        params: dict = {"uid": uid, "top_k": settings.MEMORY_HYBRID_KEYWORD_TOP_K}
        params.update({f"kw{i}": f"%{kw}%" for i, kw in enumerate(keywords)})
        sql = f"""
            SELECT m.id AS memory_id, m.fact, m.memory_type, m.privacy_level,
                   m.importance, m.confidence, m.created_at,
                   ({match_expr}) AS kw_score
            FROM memories m
            WHERE {base_conds} AND {kw_conds}
            ORDER BY kw_score DESC, m.created_at DESC
            LIMIT :top_k
        """
        kw_rows = (await session.execute(text(sql).bindparams(**params))).mappings().all()
        fused = _rrf_fuse(fused, [dict(r) for r in kw_rows], top_k)
    elif fused:
        fused = fused[:top_k]

    # ── 排序加权：RRF * (1 + importance*0.5) * 时效因子 ──
    for row in fused:
        half_life = settings.MEMORY_HALF_LIFE_DAYS.get(row.get("memory_type"))
        recency = _recency_factor(row.get("created_at"), half_life)
        importance = float(row.get("importance") or 0.0)
        row["recency"] = round(recency, 4)
        row["score"] = round((float(row.get("score") or 0.0)) * (1 + importance * 0.5) * recency, 4)

    return fused
