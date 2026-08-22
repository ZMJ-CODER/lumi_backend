"""Skill 合法池内的混合语义排序。

本模块不做授权。调用方必须先完成场景、角色、写操作和运行时过滤；索引
不可用时返回空分数，让词法/显式规则完整接管，绝不影响请求可执行性。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from loguru import logger

from app.agents.skills.capability import ToolCapability
from app.core.config import settings

_vectors: dict[str, list[float]] = {}
_build_lock = asyncio.Lock()
_ready = False
_invalidated = True


def _key(capability: ToolCapability) -> str:
    return f"{capability.name}:{capability.version}:{capability.schema_fingerprint}"


def _descriptor(capability: ToolCapability) -> str:
    schema = capability.parameters if isinstance(capability.parameters, dict) else {}
    fields = ", ".join(sorted((schema.get("properties") or {}).keys()))
    return "\n".join(
        part for part in (
            capability.name,
            capability.description,
            capability.domain,
            " ".join(capability.intent_tags),
            f"输入字段: {fields}" if fields else "",
        ) if part
    )


def invalidate_skill_semantic_index() -> None:
    global _invalidated, _ready
    _invalidated = True
    _ready = False


def schedule_skill_semantic_index(capabilities: Iterable[ToolCapability]) -> None:
    """Best-effort background warmup; never charge a user request the model-load cost."""
    if not bool(getattr(settings, "SKILL_SEMANTIC_ROUTING_ENABLED", True)) or (_ready and not _invalidated):
        return
    try:
        # Skill routing is an enhancement, not an embedding-model bootstrapper.
        # The RAG path owns model loading/download policy; until it is ready we
        # stay on deterministic lexical routing without disk/network work.
        from app.services.rag.embeddings import embedding_model_loaded

        if not embedding_model_loaded():
            return
        asyncio.get_running_loop().create_task(warm_skill_semantic_index(list(capabilities)))
    except RuntimeError:
        # Synchronous unit tests and management scripts can continue with
        # lexical routing; the next application request/startup will warm it.
        return


async def warm_skill_semantic_index(capabilities: Iterable[ToolCapability]) -> None:
    """Build vectors outside the request path. Failures leave lexical routing intact."""
    global _ready, _invalidated, _vectors
    if not bool(getattr(settings, "SKILL_SEMANTIC_ROUTING_ENABLED", True)):
        return
    items = [item for item in capabilities if item.status == "stable"]
    if not items:
        return
    async with _build_lock:
        if _ready and not _invalidated:
            return
        try:
            from app.services.rag.embeddings import embed_texts

            values = await embed_texts([_descriptor(item) for item in items])
            if len(values) != len(items):
                return
            _vectors = {_key(item): vector for item, vector in zip(items, values, strict=False) if vector}
            _ready = bool(_vectors)
            _invalidated = False
            logger.info("Skill 语义索引已就绪: {} 个能力", len(_vectors))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Skill 语义索引预热跳过，保持词法路由: {}", exc)


async def semantic_scores(request: str, capabilities: Iterable[ToolCapability]) -> dict[str, float]:
    """Return cosine scores only when the background index is already ready."""
    if not _ready or _invalidated or not request:
        return {}
    items = list(capabilities)
    try:
        from app.services.rag.embeddings import embed_query

        query = await embed_query(request)
        if not query:
            return {}
        scores: dict[str, float] = {}
        for item in items:
            vector = _vectors.get(_key(item))
            if vector and len(vector) == len(query):
                scores[item.name] = sum(left * right for left, right in zip(query, vector, strict=False))
        return scores
    except Exception as exc:  # noqa: BLE001
        logger.debug("Skill 语义路由降级为词法: {}", exc)
        return {}
