"""管理员专有接口 —— 按设计文档 3.x 管理视图."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_admin_verified_token, require_admin, require_superadmin
from app.core.exceptions import BadRequestException, ForbiddenException, NotFoundException, UnauthorizedException
from app.core.rag_config import set_rag_overrides
from app.core.security import create_admin_verified_token, verify_admin_verified_token, verify_password
from app.models.admin import LLMConfigRequest, LLMResetRequest, RAGConfigRequest, UpdateUserRequest
from app.models.db_models import ControlLog, Document, KnowledgeSpace, User
from app.models.knowledge import AdminPasswordVerifyRequest, RebuildIndexRequest
from app.services.rag import knowledge as kb

router = APIRouter()


def _mask_api_key(key: str) -> str:
    """API 密钥脱敏，仅展示头尾."""
    if not key:
        return ""
    if len(key) <= 8:
        return "***"
    return f"{key[:6]}***{key[-4:]}"


def _to_uid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


async def _load_user(db: AsyncSession, payload: dict) -> User:
    uid = _to_uid(payload.get("sub"))
    if not uid:
        raise UnauthorizedException("令牌无效")
    user = await db.get(User, uid)
    if not user:
        raise NotFoundException("用户不存在")
    return user


def _require_admin_verified(x_admin_token: str | None, payload: dict) -> None:
    """校验管理员二次验证令牌（必须属于当前登录用户）."""
    if not x_admin_token:
        raise ForbiddenException("需要管理员二次验证")
    data = verify_admin_verified_token(x_admin_token)
    if not data or str(data.get("sub")) != str(payload.get("sub")):
        raise ForbiddenException("管理员二次验证无效或已过期，请重新验证")


# ── 用户管理（超管） ──────────────────────────────────

@router.get("/users")
async def list_users(
    keyword: str = Query(default="", description="按账号/昵称模糊搜索"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_superadmin),
):
    """用户列表（超管）—— 查看所有注册用户."""
    stmt = select(User)
    if keyword:
        kw = f"%{keyword}%"
        stmt = stmt.where(or_(User.account.ilike(kw), User.username.ilike(kw)))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(stmt.order_by(User.created_at.desc()).limit(limit).offset(offset))
    ).scalars().all()
    items = [
        {
            "user_id": str(u.id),
            "username": u.username,
            "account": u.account,
            "role": u.role,
            "status": u.status,
            "prompt_id": u.prompt_id,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u in rows
    ]
    return {"code": 0, "data": {"items": items, "total": total}}


@router.patch("/users/{user_id}")
async def update_user(
    user_id: str,
    req: UpdateUserRequest,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_superadmin),
):
    """更新用户角色/状态（超管）."""
    uid = _to_uid(user_id)
    if not uid:
        raise BadRequestException("user_id 无效")
    if str(uid) == str(payload.get("sub")):
        raise BadRequestException("不能修改自己的角色/状态")
    user = await db.get(User, uid)
    if not user:
        raise NotFoundException("用户不存在")
    if req.role is not None:
        if req.role not in ("superadmin", "admin", "user"):
            raise BadRequestException("角色无效")
        user.role = req.role
    if req.status is not None:
        if req.status not in ("active", "disabled"):
            raise BadRequestException("状态无效")
        user.status = req.status
    await db.commit()
    return {"code": 0, "message": "已更新"}


# ── 二次密码验证 ─────────────────────────────────────

@router.post("/verify-password")
async def verify_admin_password(
    req: AdminPasswordVerifyRequest,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_admin),
):
    """二次密码验证，返回临时 verified_token（有效期 5 分钟）."""
    user = await _load_user(db, payload)
    if not verify_password(req.admin_password, user.password_hash):
        raise BadRequestException("密码错误")
    token = create_admin_verified_token(str(user.id), user.username)
    return {
        "code": 0,
        "data": {
            "verified_token": token,
            "expires_in": settings.ADMIN_VERIFIED_TOKEN_EXPIRE_SECONDS,
        },
    }


# ── 全局 RAG 配置（超管 + 二次验证） ─────────────────

@router.put("/rag-config")
async def update_rag_config(
    req: RAGConfigRequest,
    payload: dict = Depends(require_superadmin),
    x_admin_token: str | None = Depends(get_admin_verified_token),
):
    """全局检索参数配置：分块大小、Top-K、相似度阈值."""
    _require_admin_verified(x_admin_token, payload)
    cfg = {
        "top_k": req.top_k,
        "similarity_threshold": req.similarity_threshold,
        "space_tags": req.space_tags,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": payload.get("username") or payload.get("sub") or "admin",
    }
    await set_rag_overrides(cfg)
    return {"code": 0, "data": cfg}


# ── 公共知识库管理（超管） ────────────────────────────

@router.post("/public-kb/documents")
async def upload_public_kb_document(
    file: UploadFile = File(...),
    category: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_superadmin),
):
    """上传公共知识库文档."""
    from celery_app.tasks import process_document

    user_id = payload["sub"]
    space = (
        await db.execute(
            select(KnowledgeSpace).where(KnowledgeSpace.is_public.is_(True)).limit(1)
        )
    ).scalar_one_or_none()
    if not space:
        space = await kb.create_space(
            db, user_id, "公共知识库", "公共知识库（管理员维护）", is_public=True
        )
        await db.commit()
    filename = file.filename or "unnamed.txt"
    content = await file.read()
    doc, file_path, is_new = await kb.upload_document_file(
        db, user_id, str(space.id), filename, content, category=category or None
    )
    await db.commit()
    if is_new:
        task = process_document.apply_async(args=(
            str(doc.id), str(file_path), str(doc.user_id), str(doc.space_id), doc.category
        ))
        await kb.record_document_enqueue(db, str(doc.id), task.id)
    return {
        "code": 0,
        "data": {
            "document_id": str(doc.id),
            "filename": filename,
            "status": doc.status,
            "space_id": str(space.id),
        },
    }


@router.get("/public-kb/documents")
async def list_public_kb_documents(
    status: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_superadmin),
):
    """公共知识库文档列表."""
    stmt = (
        select(Document)
        .join(KnowledgeSpace, KnowledgeSpace.id == Document.space_id)
        .where(KnowledgeSpace.is_public.is_(True))
    )
    if status:
        stmt = stmt.where(Document.status == status)
    rows = (
        await db.execute(stmt.order_by(Document.created_at.desc()).limit(limit))
    ).scalars().all()
    items = [
        {
            "document_id": str(d.id),
            "filename": d.filename,
            "file_size": d.file_size,
            "status": d.status,
            "category": d.category,
            "chunk_count": d.chunk_count,
            "space_id": str(d.space_id),
            "created_at": d.created_at.isoformat() if d.created_at else None,
        }
        for d in rows
    ]
    return {"code": 0, "data": {"items": items, "total": len(items)}}


@router.delete("/public-kb/documents/{document_id}")
async def delete_public_kb_document(
    document_id: str,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_superadmin),
):
    """删除公共知识库文档."""
    doc = await db.get(Document, _to_uid(document_id))
    if not doc:
        raise NotFoundException("文档不存在")
    space = await db.get(KnowledgeSpace, doc.space_id)
    if not space or not space.is_public:
        raise ForbiddenException("仅公共知识库文档可在此删除")
    ok = await kb.delete_document(db, document_id, str(doc.user_id))
    if not ok:
        raise NotFoundException("文档不存在")
    await db.commit()
    return {"code": 0, "message": "已删除"}


# ── 向量库运维（管理员 + 密码验证） ──────────────────

@router.post("/knowledge/index/rebuild")
async def rebuild_index(
    req: RebuildIndexRequest,
    x_admin_token: str | None = Depends(get_admin_verified_token),
    payload: dict = Depends(require_admin),
):
    """重建向量索引."""
    _require_admin_verified(x_admin_token, payload)
    from celery_app.tasks import rebuild_index as rebuild_index_task

    rebuild_index_task.delay(req.space_id)
    return {"code": 0, "message": "索引重建任务已提交"}


@router.post("/knowledge/cleanup")
async def cleanup_knowledge(
    x_admin_token: str | None = Depends(get_admin_verified_token),
    payload: dict = Depends(require_admin),
):
    """清理冗余向量数据."""
    _require_admin_verified(x_admin_token, payload)
    from celery_app.tasks import cleanup_vectors

    cleanup_vectors.delay()
    return {"code": 0, "message": "清理任务已提交"}


# ── 操控日志摘要查看（超管） ──────────────────────────

@router.get("/control-logs/summary")
async def get_control_logs_summary(
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_superadmin),
):
    """查看所有用户的操控日志摘要（不记录对话内容，仅操作类型和时间戳）."""
    total = (
        await db.execute(select(func.count()).select_from(ControlLog))
    ).scalar_one()
    by_action_rows = (
        await db.execute(
            select(ControlLog.action, func.count().label("n"))
            .group_by(ControlLog.action)
            .order_by(func.count().desc())
        )
    ).all()
    by_action = {a: n for a, n in by_action_rows}
    recent = (
        await db.execute(
            select(ControlLog).order_by(ControlLog.created_at.desc()).limit(50)
        )
    ).scalars().all()
    recent_items = [
        {
            "log_id": str(log.id),
            "user_id": str(log.user_id),
            "action": log.action,
            "target": log.target,
            "success": log.success,
            "created_at": log.created_at.isoformat() if log.created_at else None,
        }
        for log in recent
    ]
    return {
        "code": 0,
        "data": {
            "total_operations": total,
            "by_action": by_action,
            "recent": recent_items,
        },
    }


@router.get("/llm-config")
async def get_llm_config_view(
    scene: str | None = Query(default=None, description="场景标识；缺省表示全局默认"),
    payload: dict = Depends(require_superadmin),
):
    """查看当前生效的 LLM 配置（api_key 脱敏）."""
    from app.core.llm_config import get_llm_config

    cfg = await get_llm_config(scene)
    return {"code": 0, "data": {**cfg, "api_key": _mask_api_key(cfg.get("api_key", ""))}}


@router.put("/llm-config")
async def update_llm_config(
    req: LLMConfigRequest,
    payload: dict = Depends(require_superadmin),
):
    """更新 LLM 动态配置：先验证连通性，通过后才写入 Redis，立即生效."""
    from app.core.llm_config import get_llm_config, set_llm_config, validate_llm_config

    current = await get_llm_config(req.scene)
    candidate = {
        "base_url": req.base_url or current.get("base_url", ""),
        "api_key": req.api_key if req.api_key is not None else current.get("api_key", ""),
        "model": req.model or current.get("model", ""),
        "timeout": req.timeout if req.timeout is not None else current.get("timeout", 120),
        "source": "redis",
    }

    ok, err = await validate_llm_config(candidate)
    if not ok:
        raise BadRequestException(f"新配置验证失败，未写入: {err}")

    candidate["updated_at"] = datetime.now(timezone.utc).isoformat()
    candidate["updated_by"] = payload.get("username") or payload.get("sub") or "admin"
    await set_llm_config(candidate, req.scene)

    return {"code": 0, "data": {**candidate, "api_key": _mask_api_key(candidate["api_key"])}}


@router.post("/llm-config/reset")
async def reset_llm_config_view(
    req: LLMResetRequest,
    payload: dict = Depends(require_superadmin),
):
    """删除 Redis 中的 LLM 配置，回落 .env 默认值."""
    from app.core.llm_config import reset_llm_config

    await reset_llm_config(req.scene)
    scope = f"场景 {req.scene}" if req.scene else "全局"
    return {"code": 0, "message": f"已重置{scope} LLM 配置，回落 .env 默认值"}


# ── 技能插件管理（热更新） ─────────────────────────────

@router.get("/skills")
async def list_skills_view(payload: dict = Depends(require_admin)):
    """列出当前已注册的全部技能（含来源：builtin / plugin）."""
    from app.agents.skills.registry import SkillRegistry

    items = [
        {
            "name": s.name,
            "version": s.version,
            "status": s.status,
            "schema_fingerprint": s.schema_fingerprint,
            "replacement_skill_id": s.replacement_skill_id,
            "category": s.category,
            "environment": s.environment,
            "permission": s.permission,
            "requires_confirmation": s.requires_confirmation,
            "scenes": s.scenes,
            "source": SkillRegistry.get_source(s.name),
        }
        for s in SkillRegistry.list()
    ]
    return {"code": 0, "data": {"items": items}}


@router.post("/skills/reload")
async def reload_skills_view(payload: dict = Depends(require_admin)):
    """热更新技能插件：卸载已加载插件 → 重新扫描 plugins/skills 目录注册.

    不重启进程即可生效；适合开发迭代与线上小步更新。
    """
    from app.agents.skills.loader import rebuild_skill_semantic_index, reload_skill_plugins

    result = reload_skill_plugins()
    semantic_ready = await rebuild_skill_semantic_index()
    result["semantic_routing_ready"] = semantic_ready
    return {
        "code": 0,
        "data": result,
        "message": f"技能插件已热更新（卸载 {result['unloaded']} / 注册 {result['registered']}）",
    }
