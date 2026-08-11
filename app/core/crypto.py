"""长期记忆加密：AES-256-GCM + HKDF 每用户派生密钥.

- 主密钥只从 `settings.MEMORY_ENCRYPTION_KEY` 读取，`_master_key()` 是唯一入口；
  后续若接入 AWS KMS / HashiCorp Vault，只需替换该函数实现（如 KMS 信封加密），
  对外接口与存储格式不变（见设计文档 §3.3.1）；
- 每用户密钥 = HKDF-SHA256(master, info="lumi-memory:v{key_version}:{user_id}")：
  单用户密钥泄露不影响全库；不同密钥版本派生不同密钥，支持轮换；
- 每条记录：12 字节随机 nonce，存储 `base64(nonce || ciphertext || tag)`；
  user_id 作为 AAD 绑定密文，防止跨用户密文互换；
- 审计用 `hash_memory_text()` 只记录哈希，不记录明文。
"""

import base64
import hashlib
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import settings

NONCE_SIZE = 12
KEY_LENGTH = 32
INFO_PREFIX = b"lumi-memory:"


class MemoryDecryptError(Exception):
    """解密失败：密钥版本不匹配 / 密文损坏 / 主密钥未配置."""


def _master_key() -> bytes:
    """读取主密钥（唯一入口；KMS/Vault 扩展点）."""
    raw = settings.MEMORY_ENCRYPTION_KEY
    if not raw:
        raise MemoryDecryptError("MEMORY_ENCRYPTION_KEY 未配置，记忆加密不可用")
    try:
        key = base64.b64decode(raw)
    except Exception as exc:  # noqa: BLE001
        raise MemoryDecryptError("MEMORY_ENCRYPTION_KEY 不是合法的 base64") from exc
    if len(key) != KEY_LENGTH:
        raise MemoryDecryptError(f"MEMORY_ENCRYPTION_KEY 长度应为 {KEY_LENGTH} 字节")
    return key


def _user_key(user_id: str, key_version: int) -> bytes:
    """按用户 + 密钥版本派生密钥（轮换后旧版本仍可解旧密文）."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_LENGTH,
        salt=None,
        info=INFO_PREFIX + str(key_version).encode("ascii") + b":" + str(user_id).encode("utf-8"),
    )
    return hkdf.derive(_master_key())


def encrypt_memory_text(plaintext: str, user_id: str) -> tuple[str, int]:
    """加密记忆文本。返回 (base64(nonce||ct||tag), key_version)."""
    key_version = settings.MEMORY_ENCRYPTION_KEY_VERSION
    key = _user_key(user_id, key_version)
    nonce = secrets.token_bytes(NONCE_SIZE)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), str(user_id).encode("utf-8"))
    return base64.b64encode(nonce + ct).decode("ascii"), key_version


def decrypt_memory_text(blob: str, user_id: str, key_version: int) -> str:
    """解密记忆文本；失败抛 MemoryDecryptError（不吞异常，避免脏数据静默）."""
    try:
        raw = base64.b64decode(blob)
        if len(raw) <= NONCE_SIZE:
            raise ValueError("密文长度非法")
        nonce, ct = raw[:NONCE_SIZE], raw[NONCE_SIZE:]
        key = _user_key(user_id, key_version)
        plaintext = AESGCM(key).decrypt(nonce, ct, str(user_id).encode("utf-8"))
        return plaintext.decode("utf-8")
    except MemoryDecryptError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise MemoryDecryptError(f"解密失败: {type(exc).__name__}") from exc


def hash_memory_text(text: str) -> str:
    """审计用：返回文本 SHA-256 哈希（日志只存哈希，不存明文）."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
