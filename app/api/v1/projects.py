"""本地项目 API（方案 A）：注册结构索引 / 列表 / 删除 / 检索定位."""

from fastapi import APIRouter, Depends, Query
from loguru import logger

from app.core.database import get_db
from app.core.deps import require_auth
from app.core.exceptions import BadRequestException, NotFoundException
from app.models.project import (
    RegisterProjectRequest,
    UpdateProjectRequest,
    UploadCodeChunksRequest,
    UploadCodeEmbeddingsRequest,
)
from app.services import code_embedding, project_index
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
        "vector_enabled": p.vector_enabled,
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
        project.vector_enabled = req.vector_enabled
        await db.commit()
        logger.info(
            "[Project] 注册项目 user={} name={} files={} size={}",
            payload["sub"][:8],
            req.name,
            len(req.files),
            sum(f.size for f in req.files),
        )
    except ValueError as exc:
        logger.warning("[Project] 注册项目失败 user={} name={} err={}", payload["sub"][:8], req.name, exc)
        raise BadRequestException(str(exc)) from exc
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


@router.post("/{project_id}/embeddings")
async def upload_code_embeddings(
    project_id: str,
    req: UploadCodeEmbeddingsRequest,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_auth),
):
    """全量重建项目代码向量（本地嵌入后上传；服务器只存向量+混淆元数据）."""
    try:
        count = await code_embedding.upload_code_embeddings(
            db,
            payload["sub"],
            project_id,
            [i.model_dump() for i in req.items],
            req.mode,
        )
        await db.commit()
        logger.info(
            "[Project] 上传代码向量 project={} mode={} items={} 入库={}",
            project_id[:8],
            req.mode,
            len(req.items),
            count,
        )
    except PermissionError as exc:
        logger.warning("[Project] 上传代码向量无权限 project={} err={}", project_id[:8], exc)
        raise NotFoundException(str(exc)) from exc
    except ValueError as exc:
        logger.warning("[Project] 上传代码向量参数错误 project={} err={}", project_id[:8], exc)
        raise BadRequestException(str(exc)) from exc
    return {"code": 0, "data": {"count": count}, "message": f"已入库 {count} 条代码向量"}


@router.post("/{project_id}/chunks")
async def upload_code_chunks(
    project_id: str,
    req: UploadCodeChunksRequest,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_auth),
):
    """客户端分块文本 → 服务端 bge-m3 嵌入 → 存向量 → 文本即用即弃（不落库）."""
    try:
        count = await code_embedding.upload_code_chunks(
            db, payload["sub"], project_id, [i.model_dump() for i in req.items], req.mode
        )
        await db.commit()
        logger.info(
            "[Project] 上传代码块 project={} mode={} items={} 入库={}",
            project_id[:8],
            req.mode,
            len(req.items),
            count,
        )
    except PermissionError as exc:
        logger.warning("[Project] 上传代码块无权限 project={} err={}", project_id[:8], exc)
        raise NotFoundException(str(exc)) from exc
    except RuntimeError as exc:
        logger.warning("[Project] 上传代码块失败 project={} err={}", project_id[:8], exc)
        raise BadRequestException(str(exc)) from exc
    return {"code": 0, "data": {"count": count}, "message": f"已嵌入 {count} 条代码块"}


@router.patch("/{project_id}")
async def set_project_vector(
    project_id: str,
    req: UpdateProjectRequest,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_auth),
):
    """切换项目向量化开关（涉密项目可关闭，只保留结构索引）."""
    try:
        result = await code_embedding.set_vector_enabled(
            db, payload["sub"], project_id, req.vector_enabled
        )
        await db.commit()
    except PermissionError as exc:
        raise NotFoundException(str(exc)) from exc
    return {"code": 0, "data": {"vector_enabled": result}, "message": "已更新"}


@router.get("/{project_id}/search-vectors")
async def search_code_vectors(
    project_id: str,
    q: str = Query(..., min_length=1, max_length=300),
    top_k: int = Query(default=10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_auth),
):
    """语义检索代码（bge-m3 嵌入查询 → 向量相似度），返回 file_key + 函数定位."""
    try:
        items = await code_embedding.search_code_vectors(
            db, payload["sub"], project_id, q, top_k
        )
    except PermissionError as exc:
        raise NotFoundException(str(exc)) from exc
    return {"code": 0, "data": {"items": items}}
