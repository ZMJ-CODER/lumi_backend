"""健康检查：供容器编排（compose healthcheck）与外部探活使用."""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis import get_redis

router = APIRouter()


@router.get("")
async def health(db: AsyncSession = Depends(get_db)):
    """基础健康检查：DB / Redis 连通性（HTTP 始终 200，状态见 data.status）."""
    checks: dict[str, str] = {}
    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:  # noqa: BLE001
        checks["database"] = "error"
    try:
        r = get_redis()
        await r.ping()
        checks["redis"] = "ok"
    except Exception:  # noqa: BLE001
        checks["redis"] = "error"
    status = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"code": 0, "data": {"status": status, "checks": checks}}
