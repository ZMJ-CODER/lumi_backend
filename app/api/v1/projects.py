"""本地项目 API（方案 A）：注册结构索引 / 列表 / 删除 / 检索定位."""

from fastapi import APIRouter, Depends, Query

from app.core.database import get_db
from app.core.deps import require_auth
from app.core.exceptions import BadRequestException, NotFoundException
from app.models.project import RegisterProjectRequest
from app.services import project_index
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


def _project_view(p) -> dict:
    return {
        "project_id": str(p.id),
        "name": p.name,
        "root_label": p.root_label or "",
        "file_count": p.file_count,
        "total_size": p.total_size,
        "status": p.status,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


@router.post("")
async def register_project(
    req: RegisterProjectRequest,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_auth),
):
    """注册本地项目并上传结构索引（不含代码正文）."""
    try:
        project = await project_index.create_project(
            db, payload["sub"], req.name, req.root_label, req.files
        )
        await db.commit()
    except ValueError as exc:
        raise BadRequestException(str(exc))
    return {"code": 0, "data": _project_view(project), "message": "项目索引已建立"}


@router.get("")
async def list_projects(
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_auth),
):
    """我的本地项目列表."""
    projects = await project_index.list_projects(db, payload["sub"])
    return {"code": 0, "data": {"items": [_project_view(p) for p in projects]}}


@router.delete("/{project_id}")
async def delete_project(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_auth),
):
    """删除项目索引（本地代码文件不受影响）."""
    ok = await project_index.delete_project(db, payload["sub"], project_id)
    if not ok:
        raise NotFoundException("项目不存在")
    await db.commit()
    return {"code": 0, "message": "项目索引已删除"}


@router.get("/{project_id}/search")
async def search_project(
    project_id: str,
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(default=20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_auth),
):
    """在项目索引中检索相关文件（返回相对路径，供 agent 定位后用 client 技能读取）."""
    items = await project_index.search_project(db, payload["sub"], project_id, q, limit)
    return {"code": 0, "data": {"items": items}}
