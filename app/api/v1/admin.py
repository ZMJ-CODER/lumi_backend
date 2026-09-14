"""管理员专有接口 —— 按设计文档 3.x 管理视图."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from typing import Any

from app.core.deps import require_admin, require_superadmin
from app.core.exceptions import BadRequestException, ForbiddenException, NotFoundException, UnauthorizedException
from app.knowledge.config import set_rag_overrides
from app.platform.runtime.read_view_cache import invalidate_user_view
from app.platform.security.security import create_admin_verified_token, verify_password
from app.models.admin import (
    LLMConfigRequest,
    LLMResetRequest,
    ModelProfileRequest,
    ModelRoleRequest,
    ModelRolesResetRequest,
    ModelRolesTestRequest,
    ModelRolesUpdateRequest,
    RAGConfigRequest,
    StrategyPolicyToggleRequest,
    UpdateUserRequest,
)
from app.models.db_models import ControlLog, Document, KnowledgeSpace, User
from app.models.knowledge import AdminPasswordVerifyRequest, RebuildIndexRequest
from app.knowledge.retrieval import knowledge as kb

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


# ── 管理面授权口径（2026-09 裁决） ─────────────────────
#
# **只需要 superadmin JWT**：管理员已经登录过一次，查看连接状态/改配置不再要求
# 第二次输入密码。历史实现是"超管 JWT + X-Admin-Token"两级（token 由
# ``POST /admin/verify-password`` 用管理员密码换取），现场体验是"点一下测试连接
# 还要再输一遍密码"，且多窗口/多端时 token 5 分钟就过期。现已全部移除：
#
# * ``POST /admin/verify-password`` **保留**（仍校验密码、仍签发 token），
#   仅为尚未更新的旧客户端兼容，不再有接口依赖它；
# * 鉴权唯一入口仍是 ``Depends(require_superadmin)``（``require_auth`` + 角色判定）。
#
# 注意：请求里若仍带 ``X-Admin-Token`` 会被**直接忽略**（unknown header 无副作用），
# 因此新旧客户端都能用。


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
    await invalidate_user_view(str(user.id))
    return {"code": 0, "message": "已更新"}


# ── 二次密码验证（**已废弃**，仅为旧客户端保留） ──────────

@router.post("/verify-password")
async def verify_admin_password(
    req: AdminPasswordVerifyRequest,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_admin),
):
    """[已废弃] 二次密码验证，返回临时 verified_token（有效期 5 分钟）。

    管理接口**不再需要**这个 token（授权只依赖 superadmin JWT）。端点保留是为了
    旧客户端不会 404：仍然校验密码、仍然签发 token，但没有任何接口消费它。
    新客户端请直接调用管理接口，不要再弹密码框。
    """
    user = await _load_user(db, payload)
    if not verify_password(req.admin_password, user.password_hash):
        raise BadRequestException("密码错误")
    token = create_admin_verified_token(str(user.id), user.username)
    return {
        "code": 0,
        "data": {
            "verified_token": token,
            "expires_in": settings.ADMIN_VERIFIED_TOKEN_EXPIRE_SECONDS,
            "deprecated": True,
            "deprecated_reason": "管理接口已不再要求二次密码验证，请直接调用",
        },
    }


# ── 全局 RAG 配置（超管） ────────────────────────────

@router.put("/rag-config")
async def update_rag_config(
    req: RAGConfigRequest,
    payload: dict = Depends(require_superadmin),
):
    """全局检索参数配置：分块大小、Top-K、相似度阈值."""
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
    payload: dict = Depends(require_admin),
):
    """重建向量索引."""
    from celery_app.tasks import rebuild_index as rebuild_index_task

    rebuild_index_task.delay(req.space_id)
    return {"code": 0, "message": "索引重建任务已提交"}


@router.post("/knowledge/cleanup")
async def cleanup_knowledge(
    payload: dict = Depends(require_admin),
):
    """清理冗余向量数据."""
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
    from app.platform.model.llm_config import get_llm_config

    cfg = await get_llm_config(scene)
    return {"code": 0, "data": {**cfg, "api_key": _mask_api_key(cfg.get("api_key", ""))}}


@router.put("/llm-config")
async def update_llm_config(
    req: LLMConfigRequest,
    payload: dict = Depends(require_superadmin),
):
    """更新 LLM 动态配置：先验证连通性，通过后才写入 Redis，立即生效."""
    from app.platform.model.llm_config import get_llm_config, set_llm_config, validate_llm_config

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
    from app.platform.model.llm_config import reset_llm_config

    await reset_llm_config(req.scene)
    scope = f"场景 {req.scene}" if req.scene else "全局"
    return {"code": 0, "message": f"已重置{scope} LLM 配置，回落 .env 默认值"}


# ── 模型档位 / 职责角色（方案 §五、§十：后台一处切换所有同档位功能）──
#
# 路径以**前端契约**为准（``src/services/adminSystem.js::LLM_CONFIG_ENDPOINTS``）：
#   GET  /admin/llm-config/models       读取档位 + 角色映射
#   PUT  /admin/llm-config/models       保存（body: {profiles?:{...}, roles?:{...}}）
#   POST /admin/llm-config/models/reset 重置（body: {profile?}；空=全部）
#   POST /admin/llm-config/models/test  连通性测试（body: {profile}）
# 全部只需超管 JWT（二次密码验证已于 2026-09 移除）。
# 旧的 ``/admin/model-roles*`` 保留为兼容别名（同语义）。


def _profile_payload(profiles: Any, roles: Any) -> tuple[dict[str, dict], dict[str, str]]:
    """解析前端提交的 ``{profiles, roles}``（只取被改动的部分）。"""
    profile_updates: dict[str, dict] = {}
    role_updates: dict[str, str] = {}
    if isinstance(profiles, dict):
        for name, cfg in profiles.items():
            if isinstance(cfg, dict):
                profile_updates[str(name)] = dict(cfg)
    if isinstance(roles, dict):
        for role, profile in roles.items():
            role_updates[str(role)] = str(profile or "")
    return profile_updates, role_updates


async def _apply_role_config(profile_updates: dict[str, dict], role_updates: dict[str, str]) -> dict:
    """写入档位覆盖与角色映射；返回生效摘要（供回执/审计）。"""
    from app.platform.model.model_roles import ALL_PROFILES, set_profile_config, set_role_profile

    applied_profiles: list[str] = []
    applied_roles: list[str] = []
    for name, cfg in profile_updates.items():
        if name not in ALL_PROFILES:
            raise BadRequestException(f"未知模型档位：{name}")
        await set_profile_config(name, cfg)
        applied_profiles.append(name)
    for role, profile in role_updates.items():
        try:
            await set_role_profile(role, profile or None)
        except ValueError as exc:
            raise BadRequestException(str(exc)) from exc
        applied_roles.append(role)
    return {"profiles": applied_profiles, "roles": applied_roles}


@router.get("/llm-config/models")
async def get_llm_models_view(payload: dict = Depends(require_superadmin)):
    """当前生效的档位与角色映射（密钥只回脱敏文本，管理页读取用）。"""
    from app.platform.model.model_roles import role_config_view

    return {"code": 0, "data": await role_config_view()}


@router.put("/llm-config/models")
async def update_llm_models(
    req: ModelRolesUpdateRequest,
    payload: dict = Depends(require_superadmin),
):
    """保存档位 / 角色映射（只提交被改动的部分，写 Redis 后立即生效、无需重启）."""
    profile_updates, role_updates = _profile_payload(req.profiles, req.roles)
    if not profile_updates and not role_updates:
        raise BadRequestException("没有需要保存的改动")
    applied = await _apply_role_config(profile_updates, role_updates)
    parts = []
    if applied["profiles"]:
        parts.append("档位 " + "、".join(applied["profiles"]))
    if applied["roles"]:
        parts.append(f"{len(applied['roles'])} 个角色映射")
    return {"code": 0, "data": {**applied, "message": f"已保存：{'；'.join(parts)}（立即生效）"}}


@router.post("/llm-config/models/reset")
async def reset_llm_models(
    req: ModelRolesResetRequest,
    payload: dict = Depends(require_superadmin),
):
    """重置为 .env 默认：给了 ``profile`` 只重置该档位，否则清空全部动态覆盖."""
    from app.platform.model.model_roles import (
        ALL_PROFILES,
        ALL_ROLES,
        CHAT_PROFILES,
        set_profile_config,
        set_role_profile,
    )

    if req.profile:
        if req.profile not in CHAT_PROFILES:
            raise BadRequestException(f"未知模型档位：{req.profile}")
        await set_profile_config(req.profile, None)
        return {"code": 0, "data": {"message": f"已重置 {req.profile} 档位为 .env 默认"}}
    for profile in ALL_PROFILES:
        await set_profile_config(profile, None)
    for role in ALL_ROLES:
        await set_role_profile(role, None)
    return {"code": 0, "data": {"message": "已恢复全部模型档位/角色为 .env 默认"}}


@router.post("/llm-config/models/test")
async def test_llm_model_profile(
    req: ModelRolesTestRequest,
    payload: dict = Depends(require_superadmin),
):
    """测试某档位连通性（不返回密钥）；结果写入状态供管理页展示."""
    import time

    from app.platform.model.llm_config import validate_llm_config
    from app.platform.model.model_roles import CHAT_PROFILES, resolve_role, set_profile_status

    profile = str(req.profile or "").strip()
    if not profile:
        raise BadRequestException("缺少 profile")
    if profile not in CHAT_PROFILES:
        raise BadRequestException(f"未知模型档位：{profile}")
    role_of = {
        "cheap": "title",
        "reasoning": "code_reviewer",
        "vision": "vision",
    }.get(profile, "direct_answer")
    resolved = await resolve_role(role_of)
    started = time.perf_counter()
    ok, err = await validate_llm_config({
        "base_url": resolved.base_url,
        "api_key": resolved.api_key,
        "model": resolved.model,
        "timeout": min(float(resolved.timeout or 120.0), 15.0),
    })
    latency_ms = int((time.perf_counter() - started) * 1000)
    await set_profile_status(profile, {
        "status": "ok" if ok else "error",
        "error": "" if ok else err,
        "latency_ms": latency_ms if ok else None,
    })
    return {"code": 0, "data": {"ok": ok, "error": err, "latency_ms": latency_ms, "profile": profile}}


# ── 兼容别名（旧路径；语义与上面完全一致）──


@router.get("/model-roles")
async def get_model_roles_view(payload: dict = Depends(require_superadmin)):
    """（兼容别名）当前生效的档位与角色配置。"""
    from app.platform.model.model_roles import role_config_view

    return {"code": 0, "data": await role_config_view()}


@router.put("/model-roles/profile/{profile}")
async def update_model_profile(
    profile: str,
    req: ModelProfileRequest,
    payload: dict = Depends(require_superadmin),
):
    """（兼容别名）覆盖一个档位的 provider/base_url/api_key/model 与能力声明。"""
    from app.platform.model.model_roles import resolve_role, set_profile_config

    if req.reset:
        await set_profile_config(profile, None)
        return {"code": 0, "data": {"message": f"已清除 {profile} 档位的动态覆盖（回落 .env）"}}

    candidate: dict = {}
    if req.provider:
        candidate["provider"] = req.provider
    if req.base_url:
        candidate["base_url"] = req.base_url
    if req.model:
        candidate["model"] = req.model
    if req.api_key:
        candidate["api_key"] = req.api_key
    if req.timeout is not None:
        candidate["timeout_ms"] = float(req.timeout) * 1000
    for key in ("max_output_tokens", "max_context_tokens", "supports_tools",
                "supports_json", "supports_vision", "supports_reasoning"):
        value = getattr(req, key, None)
        if value is not None:
            candidate[key] = value
    if not candidate and not req.test_only:
        raise BadRequestException("没有可更新的字段")

    if req.test_only or req.check_connection:
        from app.platform.model.llm_config import validate_llm_config

        probe = {
            "base_url": candidate.get("base_url") or "",
            "api_key": candidate.get("api_key") or "",
            "model": candidate.get("model") or "",
            "timeout": min(float(candidate.get("timeout_ms") or 0) / 1000.0 or 120.0, 15.0),
        }
        if not (probe["base_url"] and probe["api_key"] and probe["model"]):
            current = await resolve_role("direct_answer")
            probe["base_url"] = probe["base_url"] or current.base_url
            probe["api_key"] = probe["api_key"] or current.api_key
            probe["model"] = probe["model"] or current.model
        ok, err = await validate_llm_config(probe)
        if req.test_only:
            return {"code": 0, "data": {"ok": ok, "error": err}}
        if not ok:
            raise BadRequestException(f"候选配置验证失败，未写入: {err}")

    await set_profile_config(profile, candidate)
    return {"code": 0, "data": {"message": f"已更新 {profile} 档位配置（立即生效，无需重启）"}}


@router.put("/model-roles/role/{role}")
async def update_model_role(
    role: str,
    req: ModelRoleRequest,
    payload: dict = Depends(require_superadmin),
):
    """（兼容别名）把一个逻辑角色固定到某个档位（空 = 清除，回落 LLM_ROLE_*）."""
    from app.platform.model.model_roles import set_role_profile

    try:
        await set_role_profile(role, req.profile)
    except ValueError as exc:
        raise BadRequestException(str(exc)) from exc
    scope = req.profile or "默认（LLM_ROLE_*）"
    return {"code": 0, "data": {"message": f"角色 {role} 已指向 {scope}"}}


@router.post("/model-roles/reset")
async def reset_model_roles(
    payload: dict = Depends(require_superadmin),
):
    """（兼容别名）一键恢复：清除全部角色与档位的动态覆盖（回落 .env）。"""
    from app.platform.model.model_roles import ALL_PROFILES, ALL_ROLES, set_profile_config, set_role_profile

    for role in ALL_ROLES:
        await set_role_profile(role, None)
    for profile in ALL_PROFILES:
        await set_profile_config(profile, None)
    return {"code": 0, "data": {"message": "已恢复全部模型角色/档位为 .env 默认配置"}}


# ── 编排策略管理（管理员） ───────────────────────────

@router.get("/strategy-policies")
async def list_strategy_policies(
    payload: dict = Depends(require_admin),
):
    """查看独立策略文件及当前加载状态。"""
    from app.agents.orchestration.planning.strategy_engine import strategy_engine

    return {"code": 0, "data": await strategy_engine.inspect()}


@router.post("/strategy-policies/reload")
async def reload_strategy_policies(
    payload: dict = Depends(require_admin),
):
    """重新校验并原子加载策略目录；无可用策略时自动使用内置安全兜底。"""
    from app.agents.orchestration.planning.strategy_engine import strategy_engine

    return {"code": 0, "data": await strategy_engine.reload(), "message": "策略已重新加载"}


@router.post("/strategy-policies/unload")
async def unload_strategy_policy(
    req: StrategyPolicyToggleRequest,
    payload: dict = Depends(require_admin),
):
    """卸载一条策略；若没有其他策略，引擎继续使用内置安全兜底。"""
    from app.agents.orchestration.planning.strategy_engine import strategy_engine

    try:
        data = await strategy_engine.unload(req.policy_id)
    except KeyError as exc:
        raise NotFoundException("策略不存在") from exc
    except ValueError as exc:
        raise BadRequestException(str(exc)) from exc
    return {"code": 0, "data": data, "message": f"策略 {req.policy_id} 已卸载"}


@router.post("/strategy-policies/load")
async def load_strategy_policy(
    req: StrategyPolicyToggleRequest,
    payload: dict = Depends(require_admin),
):
    """恢复一条已卸载的策略文件。"""
    from app.agents.orchestration.planning.strategy_engine import strategy_engine

    try:
        data = await strategy_engine.load(req.policy_id)
    except KeyError as exc:
        raise NotFoundException("策略不存在") from exc
    return {"code": 0, "data": data, "message": f"策略 {req.policy_id} 已加载"}


# ── 技能插件管理（热更新） ─────────────────────────────

@router.get("/skills")
async def list_skills_view(payload: dict = Depends(require_admin)):
    """分别列出原子工具和组合工作流 Skill。"""
    from app.agents.skills.registry import SkillRegistry, ToolRegistry

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
            "source": ToolRegistry.get_source(s.name),
        }
        for s in ToolRegistry.list()
    ]
    workflows = [
        {
            "name": skill.name,
            "version": skill.version,
            "status": skill.status,
            "category": skill.category,
            "scenes": skill.scenes,
            "allowed_tools": skill.allowed_tools,
            "source": SkillRegistry.get_source(skill.name),
        }
        for skill in SkillRegistry.list()
    ]
    return {"code": 0, "data": {"tools": items, "workflow_skills": workflows}}


@router.post("/skills/reload")
async def reload_skills_view(payload: dict = Depends(require_admin)):
    """热更新：卸载后分别扫描 plugins/tools 与 plugins/workflows。

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
