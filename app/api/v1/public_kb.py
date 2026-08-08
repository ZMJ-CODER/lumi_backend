"""公共知识库查询 API."""

import time

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import require_auth
from app.models.admin import PublicKBSearchRequest
from app.services.rag import knowledge as kb

router = APIRouter()


@router.post("/search")
async def search_public_kb(
    req: PublicKBSearchRequest,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_auth),
):
    """公共知识库向量检索."""
    t0 = time.perf_counter()
    results = await kb.search_public_vectors(
        db,
        req.query_vector,
        req.space_tags or None,
        req.top_k,
        settings.RAG_SIMILARITY_THRESHOLD,
    )
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return {"code": 0, "data": {"results": results, "query_time_ms": elapsed_ms}}
