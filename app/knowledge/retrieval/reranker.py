"""可选的 RAG cross-encoder 重排器。

模型只在显式开启且确实有候选时懒加载；任何模型不可用都降级为原有 RRF
顺序，不能让重排成为回答链路的单点故障。
"""

from __future__ import annotations

from loguru import logger

from app.core.config import settings

_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import CrossEncoder

        logger.info(
            "⏳ 加载 RAG 重排序模型 {} (device={}) ...",
            settings.RAG_RERANK_MODEL,
            settings.RAG_RERANK_DEVICE,
        )
        _model = CrossEncoder(settings.RAG_RERANK_MODEL, device=settings.RAG_RERANK_DEVICE)
        logger.info(
            "✅ RAG 重排序模型加载完成 (device={})",
            settings.RAG_RERANK_DEVICE,
        )
    return _model


def rerank(query: str, rows: list[dict], top_k: int) -> list[dict]:
    if not query.strip() or not rows or not settings.RAG_RERANK_ENABLED:
        return rows[:top_k]
    try:
        model = _get_model()
        scores = model.predict([(query, str(row.get("chunk_text") or "")) for row in rows])
        ranked = []
        for row, score in zip(rows, scores, strict=False):
            item = dict(row)
            item["rerank_score"] = round(float(score), 6)
            ranked.append(item)
        ranked.sort(key=lambda item: item["rerank_score"], reverse=True)
        logger.debug("RAG 重排序完成: candidates={} final={}", len(rows), top_k)
        return ranked[:top_k]
    except Exception as exc:  # noqa: BLE001
        logger.warning("RAG rerank 不可用，回退 RRF 顺序: {}", str(exc)[:200])
        return rows[:top_k]
