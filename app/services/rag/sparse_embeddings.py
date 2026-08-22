"""BGE-M3 sparse 实验适配器（离线评测用，默认不进入生产检索）。

项目线上 dense 适配器基于 sentence-transformers；sparse lexical weights 需要
FlagEmbedding 的 BGEM3FlagModel，两个接口不能混为一谈。本模块用一个明确的
EmbeddingResult 契约承接副产品，缺少依赖或模型时返回空结果而不影响线上 RAG。
"""

from __future__ import annotations

from dataclasses import dataclass
import threading

from loguru import logger

from app.core.config import settings


@dataclass(slots=True)
class SparseEmbeddingResult:
    dense: list[float]
    sparse: dict[str, float]
    usage: dict[str, int]


_model = None
_lock = threading.Lock()


def _get_model():
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from FlagEmbedding import BGEM3FlagModel

                _model = BGEM3FlagModel(
                    settings.RAG_SPARSE_MODEL,
                    use_fp16=settings.RAG_SPARSE_DEVICE == "cuda",
                )
    return _model


def _encode_sync(texts: list[str]) -> list[SparseEmbeddingResult]:
    if not settings.RAG_SPARSE_EXPERIMENT_ENABLED or not texts:
        return []
    try:
        output = _get_model().encode(
            texts,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
        )
        dense = output.get("dense_vecs")
        sparse = output.get("lexical_weights")
        dense = [] if dense is None else dense
        sparse = [] if sparse is None else sparse
        return [
            SparseEmbeddingResult(
                dense=list(map(float, dense[index])),
                sparse={str(key): float(value) for key, value in (sparse[index] or {}).items()},
                usage={"input": len(texts[index])},
            )
            for index in range(min(len(dense), len(sparse)))
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("BGE-M3 sparse 实验接口不可用: {}", str(exc)[:240])
        return []


async def embed_sparse_texts(texts: list[str]) -> list[SparseEmbeddingResult]:
    """离线评测入口；不会被普通 RAG 查询调用。"""
    from app.core.executors import run_in_compute

    return await run_in_compute(_encode_sync, list(texts))
