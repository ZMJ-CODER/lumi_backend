"""知识库模块 API —— 知识空间 / 文档上传 / 列表 / 删除."""

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_auth
from app.core.exceptions import BadRequestException, ForbiddenException, NotFoundException
from app.models.knowledge import CreateSpaceRequest, UpdateSpaceRequest
from app.services import knowledge_service as kb

router = APIRouter()


def _role(payload: dict) -> str:
    return payload.get("role", "user")


def _is_admin(payload: dict) -> bool:
    return _role(payload) in ("admin", "superadmin")


# ─── 文档管理 ────────────────────────────────────────

@router.post("/documents")
async def upload_document(
    file: UploadFile = File(...),
    space_id: str = Form(...),
    scene_tag: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_auth),
):
    """上传文档到知识库，异步处理分块与向量化."""
    user_id = payload["sub"]
    content = await file.read()
    try:
        doc, file_path = await kb.upload_document_file(
            db, user_id, space_id, file.filename or "unnamed.txt", content
        )
    except ValueError as e:
        raise BadRequestException(str(e))
    except LookupError as e:
        raise NotFoundException(str(e))
    except PermissionError as e:
        raise ForbiddenException(str(e))

    # 先提交，再入队，避免 Celery 任务读到未提交的文档记录
    await db.commit()

    from celery_app.tasks import process_document

    process_document.delay(str(doc.id), str(file_path), str(doc.user_id), str(doc.space_id))
    return {
        "code": 0,
        "data": {
            "document_id": str(doc.id),
            "filename": doc.filename,
            "status": doc.status,
            "chunk_count": doc.chunk_count,
            "space_id": str(doc.space_id),
        },
    }


@router.get("/documents")
async def list_documents(
    space_id: str = Query(...),
    status: str = Query(default="ready"),
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_auth),
):
    """获取我的文档列表."""
    try:
        items = await kb.list_documents(db, payload["sub"], space_id, status, limit)
    except LookupError as e:
        raise NotFoundException(str(e))
    except PermissionError as e:
        raise ForbiddenException(str(e))
    return {"code": 0, "data": {"items": items, "total": len(items)}}


@router.delete("/documents/{document_id}")
async def delete_document(document_id: str, db: AsyncSession = Depends(get_db), payload: dict = Depends(require_auth)):
    """删除文档及向量."""
    try:
        await kb.delete_document(db, document_id, payload["sub"])
    except LookupError as e:
        raise NotFoundException(str(e))
    except PermissionError as e:
        raise ForbiddenException(str(e))
    await db.commit()
    return {"code": 0, "message": "已删除"}


# ─── 知识空间管理 ─────────────────────────────────────

@router.post("/spaces")
async def create_space(
    req: CreateSpaceRequest,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_auth),
):
    """创建知识空间."""
    try:
        space = await kb.create_space(
            db,
            payload["sub"],
            req.name,
            req.description,
            req.scene_tag,
            is_public=req.is_public if _is_admin(payload) else False,
        )
    except ValueError as e:
        raise BadRequestException(str(e))
    await db.commit()
    return {
        "code": 0,
        "data": {
            "space_id": str(space.id),
            "name": space.name,
            "scene_tag": space.scene_tag,
            "is_public": space.is_public,
        },
    }


@router.get("/spaces")
async def list_spaces(db: AsyncSession = Depends(get_db), payload: dict = Depends(require_auth)):
    """我的知识空间列表（含公共空间）."""
    items = await kb.list_spaces(db, payload["sub"])
    return {"code": 0, "data": {"items": items, "total": len(items)}}


@router.patch("/spaces/{space_id}")
async def update_space(
    space_id: str,
    req: UpdateSpaceRequest,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_auth),
):
    """更新知识空间."""
    try:
        await kb.update_space(
            db,
            space_id,
            payload["sub"],
            name=req.name,
            description=req.description,
            scene_tag=req.scene_tag,
            is_public=req.is_public,
            is_admin=_is_admin(payload),
        )
    except LookupError as e:
        raise NotFoundException(str(e))
    except PermissionError as e:
        raise ForbiddenException(str(e))
    except ValueError as e:
        raise BadRequestException(str(e))
    await db.commit()
    return {"code": 0, "message": "已更新"}


@router.delete("/spaces/{space_id}")
async def delete_space(
    space_id: str,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_auth),
):
    """删除知识空间（级联删除文档与向量）."""
    try:
        await kb.delete_space(db, space_id, payload["sub"])
    except LookupError as e:
        raise NotFoundException(str(e))
    except PermissionError as e:
        raise ForbiddenException(str(e))
    await db.commit()
    return {"code": 0, "message": "已删除"}
