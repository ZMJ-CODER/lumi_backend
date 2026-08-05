"""操控日志模块 API."""

from fastapi import APIRouter, Depends, Query

from app.core.deps import require_auth
from app.models.control_log import BatchUploadLogsRequest

router = APIRouter()


@router.get("")
async def get_control_logs(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=30, ge=1, le=100),
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
    payload: dict = Depends(require_auth),
):
    """获取操控日志."""
    # TODO: 分页查询操控日志
    return {"code": 0, "data": {"items": [], "total": 0, "page": page, "limit": limit}}


@router.post("/batch")
async def batch_upload_logs(req: BatchUploadLogsRequest, payload: dict = Depends(require_auth)):
    """批量上传本地操控日志."""
    # TODO: 批量写入日志
    return {"code": 0, "message": f"已接收 {len(req.logs)} 条日志"}
