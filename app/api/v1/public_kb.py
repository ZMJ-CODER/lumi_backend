"""公共知识库查询 API."""

from fastapi import APIRouter, Depends

from app.core.deps import require_auth
from app.models.admin import PublicKBSearchRequest

router = APIRouter()


@router.post("/search")
async def search_public_kb(req: PublicKBSearchRequest, payload: dict = Depends(require_auth)):
    """公共知识库向量检索."""
    # TODO: 向量相似度搜索
    return {"code": 0, "data": {"results": [], "query_time_ms": 0}}
