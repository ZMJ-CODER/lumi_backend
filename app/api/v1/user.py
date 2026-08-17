"""用户 API."""

import uuid

from fastapi import APIRouter, Depends
from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_auth
from app.core.exceptions import BadRequestException, ForbiddenException, NotFoundException, UnauthorizedException
from app.core.security import hash_password, validate_password_strength, verify_password
from app.models.db_models import (
    Attachment,
    CodeEmbedding,
    Conversation,
    DailyTokenStat,
    Document,
    DocumentChunk,
    KnowledgeSpace,
    LLMUsage,
    Memory,
    MemoryProfile,
    Message,
    OfficeSession,
    Project,
    ProjectIndex,
    RefreshToken,
    User,
    UserPreference,
    UserPreset,
    UserPrompt,
)
from app.models.user import (
    ChangePasswordRequest,
    DeleteAccountRequest,
    SetPromptRequest,
    UserProfileUpdateRequest,
)
from app.core.model_catalog import PROVIDER_BASE_URLS, find_model, get_model_catalog
from app.models.user import UserLlmConfigRequest
from app.services import user_llm_config
from app.services.prompts import get_prompt

router = APIRouter()

MAX_AVATAR_SIZE = 2 * 1024 * 1024  # 2 MB


async def _load_user(db: AsyncSession, payload: dict) -> User:
    """根据 JWT payload 加载当前用户，校验存在与状态."""
    user_id = payload.get("sub", "")
    if not user_id:
        raise UnauthorizedException("请先登录")
    try:
        user_uuid = uuid.UUID(user_id)
    except (ValueError, AttributeError):
        raise UnauthorizedException("令牌无效") from None

    result = await db.execute(select(User).where(User.id == user_uuid))
    user = result.scalar_one_or_none()
    if not user:
        raise NotFoundException("用户不存在")
    if user.status == "disabled":
        raise ForbiddenException("账号已被禁用")
    return user


def _user_dict(user: User) -> dict:
    return {
        "user_id": str(user.id),
        "username": user.username,
        "account": user.account,
        "avatar_url": user.avatar_url or "",
        "role": user.role,
        "status": user.status,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


@router.get("/me")
async def get_current_user_info(
    payload: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """获取当前用户信息（从数据库查询完整信息）."""
    user = await _load_user(db, payload)
    return {"code": 0, "data": _user_dict(user)}


@router.get("/prompt")
async def get_my_prompt(
    payload: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """获取我当前选定的角色提示词 id（空 = 场景默认）."""
    user = await _load_user(db, payload)
    return {"code": 0, "data": {"prompt_id": user.prompt_id or ""}}


@router.put("/prompt")
async def set_my_prompt(
    req: SetPromptRequest,
    payload: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """设置我使用的角色提示词（空串恢复默认）；立即对下一条消息生效."""
    user = await _load_user(db, payload)
    prompt_id = req.prompt_id.strip()
    if prompt_id and await get_prompt(prompt_id, str(user.id)) is None:
        raise BadRequestException("角色不存在")
    user.prompt_id = prompt_id or None
    await db.commit()
    return {
        "code": 0,
        "data": {"prompt_id": user.prompt_id or ""},
        "message": "已更新",
    }


@router.put("/profile")
async def update_profile(
    req: UserProfileUpdateRequest,
    payload: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """更新个人资料：昵称 / 头像."""
    user = await _load_user(db, payload)

    if req.nickname is not None:
        nickname = req.nickname.strip()
        if not nickname:
            raise BadRequestException("昵称不能为空")
        user.username = nickname

    if req.avatar_url is not None:
        avatar = req.avatar_url.strip()
        if avatar and not avatar.startswith("data:image/"):
            raise BadRequestException("头像格式无效，仅支持图片 data URL")
        if len(avatar) > MAX_AVATAR_SIZE:
            raise BadRequestException("头像图片过大（最大 2MB）")
        user.avatar_url = avatar or None

    await db.commit()
    return {"code": 0, "data": _user_dict(user)}


@router.post("/password")
async def change_password(
    req: ChangePasswordRequest,
    payload: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """修改密码：校验原密码 → 强度校验 → 更新哈希 → 撤销全部 refresh token."""
    user = await _load_user(db, payload)

    if not verify_password(req.old_password, user.password_hash):
        raise BadRequestException("原密码错误")
    if not validate_password_strength(req.new_password):
        raise BadRequestException("新密码需至少 8 位，包含字母和数字")

    user.password_hash = hash_password(req.new_password)
    # 撤销所有已签发的 refresh token，强制其他设备重新登录
    await db.execute(delete(RefreshToken).where(RefreshToken.user_id == user.id))
    await db.commit()

    return {"code": 0, "message": "密码已修改，其他设备已退出，请使用新密码重新登录"}


# ── 模型选择（办公模式） ─────────────────────────────

@router.get("/models")
async def list_models(payload: dict = Depends(require_auth)):
    """模型目录（含上下文/多模态/价格/推理强度等元数据）."""
    return {"code": 0, "data": {"items": get_model_catalog()}}


@router.get("/llm-config")
async def get_llm_config_view(payload: dict = Depends(require_auth)):
    """当前用户的模型选择（不含任何 API key）."""
    cfg = await user_llm_config.get_user_llm_config(payload["sub"])
    return {"code": 0, "data": cfg or {}}


@router.put("/llm-config")
async def set_llm_config_view(
    req: UserLlmConfigRequest,
    payload: dict = Depends(require_auth),
):
    """保存模型选择：内置模型 或 自备 API（byok）.

    BYOK 只存 provider/model/reasoning_effort；API key 由前端本地加密保存，
    每次请求临时携带 X-LLM-API-KEY 头，后端用完即弃、绝不落库。
    """
    if req.provider not in PROVIDER_BASE_URLS:
        raise BadRequestException(f"不支持的 API 提供商: {req.provider}，可选: {list(PROVIDER_BASE_URLS)}")
    model_id = (req.model or "").strip()
    if not model_id:
        raise BadRequestException("模型名称不能为空")
    if not req.byok and find_model(model_id) is None:
        raise BadRequestException(f"未知的内置模型: {model_id}")
    if req.reasoning_effort and req.reasoning_effort not in ("low", "medium", "high"):
        raise BadRequestException("推理强度仅支持 low / medium / high")

    cfg = {
        "provider": req.provider,
        "model": model_id,
        "reasoning_effort": req.reasoning_effort,
        "byok": bool(req.byok),
    }
    await user_llm_config.set_user_llm_config(payload["sub"], cfg)
    return {"code": 0, "data": cfg, "message": "模型选择已保存"}


@router.delete("/llm-config")
async def clear_llm_config_view(payload: dict = Depends(require_auth)):
    """恢复默认模型（清除用户级选择）."""
    await user_llm_config.clear_user_llm_config(payload["sub"])
    return {"code": 0, "message": "已恢复默认模型"}


@router.delete("/account")
async def delete_account(
    req: DeleteAccountRequest,
    payload: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """注销账号：校验密码后删除该用户的全部数据（会话/消息/记忆/知识库/角色/偏好等）."""
    user = await _load_user(db, payload)
    if not verify_password(req.password, user.password_hash):
        raise BadRequestException("密码不正确")
    uid = user.id

    try:
        # 1. 项目 / 代码索引 / 代码嵌入
        await db.execute(delete(CodeEmbedding).where(CodeEmbedding.user_id == uid))
        await db.execute(delete(ProjectIndex).where(ProjectIndex.user_id == uid))
        await db.execute(delete(Project).where(Project.user_id == uid))
        # 2. 知识库（分块 → 文档 → 空间）
        await db.execute(delete(DocumentChunk).where(DocumentChunk.user_id == uid))
        await db.execute(delete(Document).where(Document.user_id == uid))
        await db.execute(delete(KnowledgeSpace).where(KnowledgeSpace.user_id == uid))
        # 3. 长期记忆
        await db.execute(delete(Memory).where(Memory.user_id == uid))
        await db.execute(delete(MemoryProfile).where(MemoryProfile.user_id == uid))
        # 4. 办公会话
        await db.execute(delete(OfficeSession).where(OfficeSession.user_id == uid))
        # 5. 会话及其消息/附件
        conv_ids = (
            (await db.execute(select(Conversation.id).where(Conversation.user_id == uid)))
            .scalars()
            .all()
        )
        if conv_ids:
            msg_ids = select(Message.id).where(Message.conversation_id.in_(conv_ids))
            await db.execute(delete(Attachment).where(Attachment.message_id.in_(msg_ids)))
            await db.execute(delete(Message).where(Message.conversation_id.in_(conv_ids)))
            await db.execute(delete(Conversation).where(Conversation.id.in_(conv_ids)))
        # 6. 角色 / 偏好 / 方案
        await db.execute(delete(UserPrompt).where(UserPrompt.user_id == uid))
        await db.execute(delete(UserPreference).where(UserPreference.user_id == uid))
        await db.execute(delete(UserPreset).where(UserPreset.user_id == uid))
        # 7. 令牌 / 用量统计
        await db.execute(delete(RefreshToken).where(RefreshToken.user_id == uid))
        await db.execute(delete(LLMUsage).where(LLMUsage.user_id == uid))
        await db.execute(delete(DailyTokenStat).where(DailyTokenStat.user_id == uid))
        # 8. 用户行
        await db.delete(user)
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        logger.warning("注销账号失败: {}", exc)
        raise BadRequestException(f"注销失败，请稍后重试: {exc}") from exc

    # 清理用户上传文件（尽力而为，不阻塞注销）
    try:
        import asyncio
        import shutil
        from pathlib import Path

        from app.core.config import settings

        base = Path(settings.UPLOAD_DIR)
        for sub in ("chat", "tts_voice"):
            target = (base / sub / str(uid)).resolve()
            if base.resolve() in target.parents and target.exists():
                await asyncio.to_thread(shutil.rmtree, target, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass

    return {"code": 0, "data": {"deleted": True}, "message": "账号已注销"}
