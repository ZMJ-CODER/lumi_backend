"""本地同步 API."""

from fastapi import APIRouter, Depends

from app.core.deps import require_auth
from app.models.admin import SyncSummaryRequest

router = APIRouter()


@router.post("/sync-summary")
async def sync_summary(req: SyncSummaryRequest, payload: dict = Depends(require_auth)):
    """同步游戏模式对话摘要."""
    # TODO: 存储对话摘要
    return {"code": 0, "message": f"已同步 {len(req.summaries)} 条摘要"}
