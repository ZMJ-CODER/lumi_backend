"""用户 API."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_auth
from app.models.db_models import User

router = APIRouter()


@router.get("/me")
async def get_current_user_info(
    payload: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """获取当前用户信息（从数据库查询完整信息）."""
    user_id = payload.get("sub", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="请先登录")

    try:
        user_uuid = uuid.UUID(user_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=401, detail="令牌无效")

    result = await db.execute(select(User).where(User.id == user_uuid))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.status == "disabled":
        raise HTTPException(status_code=403, detail="账号已被禁用")

    return {
        "code": 0,
        "data": {
            "user_id": str(user.id),
            "username": user.username,
            "account": user.account,
            "avatar_url": user.avatar_url or "",
            "role": user.role,
            "status": user.status,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        },
    }