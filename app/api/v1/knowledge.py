"""知识库模块 API."""

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from app.core.deps import require_auth, require_admin
from app.models.knowledge import CreateSpaceRequest, UpdateSpaceRequest

router = APIRouter()


# ─── 文档管理 ────────────────────────────────────────

@router.post("/documents")
async def upload_document(
    file: UploadFile = File(...),
    space_id: str = Form(...),
    scene_tag: str = Form(default=""),
    payload: dict = Depends(require_auth),
):
    """上传文档到知识库."""
    # TODO: 处理文件上传、解析、入库
    return {"code": 0, "data": {"document_id": "placeholder-doc-id"}}


@router.get("/documents")
async def list_documents(
    space_id: str = Query(...),
    status: str = Query(default="ready"),
    limit: int = Query(default=20, ge=1, le=100),
    payload: dict = Depends(require_auth),
):
    """获取我的文档列表."""
    # TODO: 分页查询文档
    return {"code": 0, "data": {"items": [], "total": 0}}


@router.delete("/documents/{document_id}")
async def delete_document(document_id: str, payload: dict = Depends(require_auth)):
    """删除文档."""
    # TODO: 删除文档及向量
    return {"code": 0, "message": "已删除"}


# ─── 知识空间管理（管理员） ──────────────────────────

@router.post("/spaces")
async def create_space(req: CreateSpaceRequest, payload: dict = Depends(require_admin)):
    """创建知识空间."""
    return {"code": 0, "data": {"space_id": "placeholder-space-id"}}


@router.get("/spaces")
async def list_spaces(payload: dict = Depends(require_admin)):
    """知识空间列表."""
    return {"code": 0, "data": {"items": []}}


@router.patch("/spaces/{space_id}")
async def update_space(space_id: str, req: UpdateSpaceRequest, payload: dict = Depends(require_admin)):
    """更新知识空间."""
    return {"code": 0, "message": "已更新"}


@router.delete("/spaces/{space_id}")
async def delete_space(space_id: str, payload: dict = Depends(require_admin)):
    """删除知识空间（需二次验证）."""
    # TODO: 校验 X-Admin-Token
    return {"code": 0, "message": "已删除"}
