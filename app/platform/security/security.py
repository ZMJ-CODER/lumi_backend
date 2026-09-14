"""安全相关：密码哈希、JWT、refresh_token 管理.

服务端单向哈希方案 v1.0:
  - 密码通过 HTTPS 传输
  - 服务端 argon2id 加盐哈希存储
  - refresh_token 数据库仅存哈希值
"""

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

from jose import jwt
from jose.exceptions import ExpiredSignatureError, JWTError
from loguru import logger
from passlib.hash import argon2

from app.core.config import settings

# argon2id 参数（与设计文档 2.3 一致：memory=65536KB, iterations=3, parallelism=4, hashLength=32）
_ARGON2_PARAMS = {
    "memory_cost": 65536,  # 64 MB
    "time_cost": 3,        # 3 次迭代
    "parallelism": 4,      # 4 并行度
    "hash_len": 32,        # 32 字节哈希
    "salt_len": 16,        # 16 字节随机盐（≥128 位）
}


# ── 密码强度校验 ────────────────────────────────────────

def validate_password_strength(password: str) -> bool:
    """校验密码强度: 最少 8 位，必须包含字母和数字（可配置）."""
    if len(password) < settings.PASSWORD_MIN_LENGTH:
        return False
    if settings.PASSWORD_REQUIRE_LETTER and not re.search(r"[A-Za-z]", password):
        return False
    if settings.PASSWORD_REQUIRE_DIGIT and not re.search(r"\d", password):
        return False
    return True


# ── 密码哈希 ────────────────────────────────────────────

def hash_password(password: str) -> str:
    """argon2id 哈希密码，自动生成 16 字节随机盐.

    返回 PHC 格式字符串: $argon2id$v=19$m=65536,t=3,p=4$<salt>$<hash>
    该字符串内含算法标识、参数、盐值、哈希值，便于后续自动适配算法升级。
    """
    return argon2.using(**_ARGON2_PARAMS).hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    """校验密码（自动从 stored_hash 解析算法参数）."""
    return argon2.verify(password, stored_hash)


# ── refresh_token ───────────────────────────────────────

def generate_refresh_token() -> str:
    """生成不透明 refresh_token（128 位随机数 hex）."""
    return secrets.token_hex(32)


def hash_refresh_token(token: str) -> str:
    """refresh_token 的 SHA-256 哈希，存数据库."""
    return hashlib.sha256(token.encode()).hexdigest()


# ── JWT ─────────────────────────────────────────────────

def create_access_token(user_id: str, username: str, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(seconds=settings.ACCESS_TOKEN_EXPIRE_SECONDS)
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "type": "access",
        "iat": datetime.now(timezone.utc),
        "exp": expire,
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_admin_verified_token(user_id: str, username: str = "") -> str:
    """管理员二次验证令牌（5 分钟，仅用于敏感管理操作）."""
    expire = datetime.now(timezone.utc) + timedelta(
        seconds=settings.ADMIN_VERIFIED_TOKEN_EXPIRE_SECONDS
    )
    payload = {
        "sub": user_id,
        "username": username,
        "type": "admin_verified",
        "iat": datetime.now(timezone.utc),
        "exp": expire,
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def verify_admin_verified_token(token: str) -> dict | None:
    """校验管理员二次验证令牌，返回 payload；无效/过期/类型不符返回 None."""
    try:
        data = decode_token(token)
        if data.get("type") != "admin_verified":
            return None
        return data
    except Exception:  # noqa: BLE001
        return None


def jwt_secret_fingerprint() -> str:
    """当前 JWT 密钥的**指纹**（sha256 前 8 位，永不泄露密钥本身）。

    用途只有一个：让"所有接口突然 401"这类事故可以在一行日志里定位。刷新
    ``JWT_SECRET_KEY`` 会让**所有**已发出的令牌（access 与 refresh）立即失效——
    客户端的 refresh 也会 401，于是它无法自愈，必须重新登录。启动时打印指纹，
    排障时对比"令牌签发时的指纹"与"当前指纹"就能立刻分清是**密钥轮换**、
    令牌过期，还是真的没登录。
    """
    return hashlib.sha256(str(settings.JWT_SECRET_KEY).encode("utf-8")).hexdigest()[:8]


def decode_token(token: str) -> dict:
    """校验并解出 JWT；失败时**把原因说清楚**再抛。

    这里的日志是刻意的：`require_auth` 会把异常吞成 401，如果没有这一行，
    "密钥轮换导致全站 401"在日志里只剩一串 401 状态码，无法区分原因。
    过期是**正常现象**（access 令牌只有 1 小时），因此只记 debug；
    签名不符/格式错误才是需要告警的（通常是密钥变了或令牌来自别的环境）。
    """
    try:
        return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except ExpiredSignatureError:
        logger.debug("[auth] 令牌已过期（正常现象，客户端应走 refresh）")
        raise
    except JWTError as exc:
        logger.warning(
            "[auth] 令牌校验失败（{}）：当前密钥指纹={}。"
            "若刚刚轮换过 JWT_SECRET_KEY，所有已发出的 access/refresh 令牌都会失效，"
            "客户端必须重新登录；否则请检查令牌是否来自其它环境。",
            type(exc).__name__,
            jwt_secret_fingerprint(),
        )
        raise
