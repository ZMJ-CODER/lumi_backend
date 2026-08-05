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


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
