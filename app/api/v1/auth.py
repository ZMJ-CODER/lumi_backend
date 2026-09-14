"""认证模块 API：注册、登录、刷新、登出、验证码.

服务端单向哈希方案 v1.0:
  - 密码通过 HTTPS 传输
  - 服务端 argon2id 加盐哈希存储
  - refresh_token 数据库仅存哈希值（轮换策略）
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.exceptions import BadRequestException, ConflictException, ForbiddenException, UnauthorizedException
from app.platform.security.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    validate_password_strength,
    verify_password,
)
from app.models.auth import LoginRequest, RegisterRequest, TokenRefreshRequest
from app.models.db_models import RefreshToken, User
from app.services.captcha_service import generate_captcha, verify_captcha

router = APIRouter()


# ══════════════════════════════════════════════
# 图形验证码
# ══════════════════════════════════════════════

@router.get("/captcha")
async def get_captcha(request: Request):
    """获取图形验证码（携带客户端 IP：限流 + 连续输错锁定）."""
    client_ip = request.client.host if request.client else None
    data = await generate_captcha(client_ip)
    return {"code": 0, "data": data}


# ══════════════════════════════════════════════
# 注册
# ══════════════════════════════════════════════

@router.post("/register")
async def register(
    request: Request,
    req: RegisterRequest,
    db: AsyncSession = Depends(get_db),
):
    """用户注册.

    流程: 校验验证码 → 检查账号唯一性 → 密码强度 → argon2id 哈希 → 写入 DB → 签发 JWT
    """
    client_ip = request.client.host if request.client else None
    # 1. 校验图形验证码
    if not await verify_captcha(req.captcha_id, req.captcha_result, client_ip):
        raise BadRequestException("验证码错误或已过期")

    # 2. 检查账号唯一性
    result = await db.execute(select(User).where(User.account == req.account))
    if result.scalar_one_or_none():
        raise ConflictException("账号已存在")

    # 3. 密码强度校验
    if not validate_password_strength(req.password):
        raise BadRequestException("密码需至少 8 位，包含字母和数字")

    # 4. argon2id 哈希
    password_hash = hash_password(req.password)

    # 5. 创建用户
    user = User(
        username=req.account,       # 默认昵称 = 账号
        account=req.account,
        password_hash=password_hash,
        role="user",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    user_id_str = str(user.id)
    logger.info(f"新用户注册: {req.account} (id={user_id_str})")

    # 6. 注册即登录：签发 JWT + refresh_token
    access_token = create_access_token(user_id_str, user.username, user.role)
    raw_refresh, token_hash = _create_refresh_token_record(db, user.id)

    return {
        "code": 0,
        "message": "注册成功",
        "data": {
            "user_id": user_id_str,
            "username": user.username,
            "access_token": access_token,
            "refresh_token": raw_refresh,
            "expires_in": settings.ACCESS_TOKEN_EXPIRE_SECONDS,
            "user": {
                "user_id": user_id_str,
                "username": user.username,
                "avatar_url": user.avatar_url or "",
                "role": user.role,
            },
        },
    }


# ══════════════════════════════════════════════
# 登录
# ══════════════════════════════════════════════

@router.post("/login")
async def login(
    request: Request,
    req: LoginRequest,
    db: AsyncSession = Depends(get_db),
):
    """用户登录.

    流程: 校验验证码 → 查用户 → 比对密码哈希 → 签发 JWT
    """
    client_ip = request.client.host if request.client else None
    # 1. 校验图形验证码
    if not await verify_captcha(req.captcha_id, req.captcha_result, client_ip):
        raise BadRequestException("验证码错误或已过期")

    # 2. 查找用户
    result = await db.execute(select(User).where(User.account == req.account))
    user = result.scalar_one_or_none()
    if not user:
        raise UnauthorizedException("账号或密码错误")

    if user.status == "disabled":
        raise ForbiddenException("账号已被禁用")

    # 3. 校验密码
    if not verify_password(req.password, user.password_hash):
        raise UnauthorizedException("账号或密码错误")

    user_id_str = str(user.id)

    # 4. 签发 JWT + refresh_token
    access_token = create_access_token(user_id_str, user.username, user.role)
    raw_refresh, _ = _create_refresh_token_record(db, user.id)

    logger.info(f"用户登录: {req.account} (id={user_id_str})")

    return {
        "code": 0,
        "data": {
            "access_token": access_token,
            "refresh_token": raw_refresh,
            "expires_in": settings.ACCESS_TOKEN_EXPIRE_SECONDS,
            "user": {
                "user_id": user_id_str,
                "username": user.username,
                "avatar_url": user.avatar_url or "",
                "role": user.role,
            },
        },
    }


# ══════════════════════════════════════════════
# 刷新令牌（轮换策略）
# ══════════════════════════════════════════════

@router.post("/refresh")
async def refresh(req: TokenRefreshRequest, db: AsyncSession = Depends(get_db)):
    """刷新令牌 —— 轮换策略：验证旧 token → 废弃 → 签发新对.

    **宽限期重放**：轮换会让旧 token 立刻失效，但同一个客户端可能持有过期副本
    （桌面端持久化的是上一次写盘的值、双开窗口/重复启动各刷新一次、浏览器多标签）。
    旧 token 被抢先消耗后，其余请求会拿到 401「刷新令牌无效」，客户端往往就此
    掉进"游客模式"——表现就是"页面什么都没了"（见 `docs/ARCHITECTURE_BOUNDARIES.md`
    §8.14.12）。因此轮换后的 ``_REFRESH_GRACE_SECONDS`` 秒内，旧 token 再来一次
    不再拒绝，而是**照常签发新的令牌对**（等价于幂等重放），并记一条 warning 便于排障。
    """
    old_hash = hash_refresh_token(req.refresh_token)

    # 查找并删除旧 token
    result = await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == old_hash)
    )
    token_record = result.scalar_one_or_none()
    if not token_record:
        grace_user_id = await _grace_user_id(old_hash)
        if grace_user_id is not None:
            logger.warning(
                "[auth] refresh_token 在宽限期内被重复使用（多窗口/重复启动/持久化副本过期）：user={}",
                str(grace_user_id)[:8],
            )
            user = await db.get(User, grace_user_id)
            if user and user.status != "disabled":
                return await _issue_token_pair(db, user, rotated_hash=old_hash, reuse=True)
            await _forget_grace(old_hash)
        raise UnauthorizedException(
            "刷新令牌无效，请重新登录",
            error_code="REFRESH_TOKEN_INVALID",
            data={"error_code": "REFRESH_TOKEN_INVALID", "action": "relogin"},
        )
    if token_record.expires_at < datetime.now(timezone.utc):
        await db.delete(token_record)
        await db.commit()
        raise UnauthorizedException(
            "刷新令牌已过期，请重新登录",
            error_code="REFRESH_TOKEN_EXPIRED",
            data={"error_code": "REFRESH_TOKEN_EXPIRED", "action": "relogin"},
        )

    # 废弃旧 token
    await db.delete(token_record)

    # 获取用户信息
    user_result = await db.execute(select(User).where(User.id == token_record.user_id))
    user = user_result.scalar_one_or_none()
    if not user or user.status == "disabled":
        await db.commit()
        raise ForbiddenException("用户不可用")

    return await _issue_token_pair(db, user, rotated_hash=old_hash)


# ══════════════════════════════════════════════
# 登出
# ══════════════════════════════════════════════

@router.post("/logout")
async def logout(
    req: TokenRefreshRequest,
    db: AsyncSession = Depends(get_db),
):
    """登出 —— 废弃 refresh_token."""
    token_hash = hash_refresh_token(req.refresh_token)
    result = await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    record = result.scalar_one_or_none()
    if record:
        await db.delete(record)
        await db.commit()

    return {"code": 0, "message": "已登出"}


# ══════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════

#: 旧 refresh_token 轮换后的重放宽限期（秒）。只覆盖"同一客户端把同一个 token
#: 用了两次"的竞态，不延长会话寿命：新令牌仍是完整的 30 天有效期。
_REFRESH_GRACE_SECONDS = 120
_REFRESH_GRACE_KEY = "auth:refresh_grace:{token_hash}"


async def _remember_rotated(token_hash: str, user_id) -> None:
    """记住"刚被轮换掉的 refresh_token → 用户"，供宽限期内的重放兜底。"""
    try:
        from app.core.redis import get_redis

        await get_redis().set(
            _REFRESH_GRACE_KEY.format(token_hash=token_hash),
            str(user_id),
            ex=_REFRESH_GRACE_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - 宽限期是兜底，Redis 不可用不能影响刷新
        logger.debug("[auth] 记录 refresh 宽限期失败（忽略）: {}", str(exc)[:120])


async def _grace_user_id(token_hash: str):
    try:
        from app.core.redis import get_redis

        value = await get_redis().get(_REFRESH_GRACE_KEY.format(token_hash=token_hash))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[auth] 读取 refresh 宽限期失败（忽略）: {}", str(exc)[:120])
        return None
    if not value:
        return None
    try:
        import uuid as _uuid

        return _uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


async def _forget_grace(token_hash: str) -> None:
    try:
        from app.core.redis import get_redis

        await get_redis().delete(_REFRESH_GRACE_KEY.format(token_hash=token_hash))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[auth] 清理 refresh 宽限期失败（忽略）: {}", str(exc)[:120])


async def _issue_token_pair(db: AsyncSession, user: User, *, rotated_hash: str = "", reuse: bool = False) -> dict:
    """签发新的 access/refresh 令牌对；``rotated_hash`` 用于记录轮换宽限期。"""
    user_id_str = str(user.id)
    access_token = create_access_token(user_id_str, user.username, user.role)
    raw_refresh, _ = _create_refresh_token_record(db, user.id)
    await db.commit()
    if rotated_hash:
        await _remember_rotated(rotated_hash, user.id)
    if reuse:
        logger.info("[auth] 已按宽限期重放 refresh_token 并签发新令牌: user={}", user_id_str[:8])
    return {
        "code": 0,
        "data": {
            "access_token": access_token,
            "refresh_token": raw_refresh,
            "expires_in": settings.ACCESS_TOKEN_EXPIRE_SECONDS,
            "user": {
                "user_id": user_id_str,
                "username": user.username,
                "avatar_url": user.avatar_url or "",
                "role": user.role,
            },
        },
    }


def _create_refresh_token_record(db: AsyncSession, user_id) -> tuple[str, str]:
    """创建 refresh_token 记录，返回 (原始token, 哈希)."""
    raw = generate_refresh_token()
    token_hash = hash_refresh_token(raw)
    expires = datetime.now(timezone.utc) + timedelta(seconds=settings.REFRESH_TOKEN_EXPIRE_SECONDS)
    record = RefreshToken(user_id=user_id, token_hash=token_hash, expires_at=expires)
    db.add(record)
    return raw, token_hash
