"""管理员专有接口 —— 按设计文档 3.x 管理视图."""

from fastapi import APIRouter, Depends

from app.core.deps import get_admin_verified_token, require_admin, require_superadmin
from app.models.admin import PublicKBSearchRequest, RAGConfigRequest, UpdateUserRequest
from app.models.knowledge import AdminPasswordVerifyRequest

router = APIRouter()


# ── 用户管理（超管） ──────────────────────────────────

@router.get("/users")
async def list_users(payload: dict = Depends(require_superadmin)):
    """用户列表（超管）—— 查看所有注册用户."""
    # TODO: SELECT id, username, role, is_active, created_at FROM users
    return {"code": 0, "data": {"items": [], "total": 0}}


@router.patch("/users/{user_id}")
async def update_user(user_id: str, req: UpdateUserRequest, payload: dict = Depends(require_superadmin)):
    """更新用户角色/状态（超管）."""
    # TODO: UPDATE users SET role = ?, is_active = ? WHERE id = ?
    return {"code": 0, "message": "已更新"}


# ── 二次密码验证 ─────────────────────────────────────

@router.post("/verify-password")
async def verify_admin_password(req: AdminPasswordVerifyRequest, payload: dict = Depends(require_admin)):
    """二次密码验证，返回临时 verified_token（有效期 5 分钟）."""
    # TODO: 校验管理员密码 → create_admin_verified_token
    return {"code": 0, "data": {"verified_token": "placeholder-verified-token", "expires_in": 300}}


# ── 全局 RAG 配置（超管 + 二次验证） ─────────────────

@router.put("/rag-config")
async def update_rag_config(
    req: RAGConfigRequest,
    payload: dict = Depends(require_superadmin),
    x_admin_token: str | None = Depends(get_admin_verified_token),
):
    """全局检索参数配置：分块大小、Top-K、相似度阈值."""
    if not x_admin_token:
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="需要管理员二次验证")
    # TODO: 校验 verified_token，更新全局 RAG 配置
    return {"code": 0, "data": {"top_k": req.top_k, "similarity_threshold": req.similarity_threshold}}


# ── 公共知识库管理（超管） ────────────────────────────

@router.post("/public-kb/documents")
async def upload_public_kb_document(payload: dict = Depends(require_superadmin)):
    """上传公共知识库文档."""
    # TODO: 接收文件 → Celery 异步处理 → 写入 knowledge_spaces (is_public=true)
    return {"code": 0, "data": {"document_id": "placeholder-doc-id"}}


@router.get("/public-kb/documents")
async def list_public_kb_documents(payload: dict = Depends(require_superadmin)):
    """公共知识库文档列表."""
    return {"code": 0, "data": {"items": []}}


@router.delete("/public-kb/documents/{document_id}")
async def delete_public_kb_document(document_id: str, payload: dict = Depends(require_superadmin)):
    """删除公共知识库文档."""
    return {"code": 0, "message": "已删除"}


# ── 向量库运维（管理员 + 密码验证） ──────────────────

@router.post("/knowledge/index/rebuild")
async def rebuild_index(payload: dict = Depends(require_admin)):
    """重建向量索引."""
    # TODO: 需二次验证 → Celery 异步重建 ivfflat 索引
    return {"code": 0, "message": "索引重建任务已提交"}


@router.post("/knowledge/cleanup")
async def cleanup_knowledge(payload: dict = Depends(require_admin)):
    """清理冗余向量数据."""
    # TODO: 需二次验证 → 清理已删除文档的 chunk、孤儿向量
    return {"code": 0, "message": "清理任务已提交"}


# ── 操控日志摘要查看（超管） ──────────────────────────

@router.get("/control-logs/summary")
async def get_control_logs_summary(payload: dict = Depends(require_superadmin)):
    """查看所有用户的操控日志摘要（不记录对话内容，仅操作类型和时间戳）."""
    # TODO: SELECT action, success, created_at FROM control_logs 聚合统计
    return {"code": 0, "data": {"total_operations": 0, "by_action": {}}}
