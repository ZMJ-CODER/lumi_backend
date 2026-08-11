"""用户 API."""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_auth
from app.core.exceptions import BadRequestException, ForbiddenException, NotFoundException, UnauthorizedException
from app.core.security import hash_password, validate_password_strength, verify_password
from app.models.db_models import RefreshToken, User
from app.models.user import ChangePasswordRequest, SetPromptRequest, UserProfileUpdateRequest
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
        raise UnauthorizedException("令牌无效")

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
