"""长期记忆加密工具测试（无需数据库）."""

import pytest

from app.core.crypto import (
    MemoryDecryptError,
    decrypt_memory_text,
    encrypt_memory_text,
    hash_memory_text,
)


def test_roundtrip():
    blob, ver = encrypt_memory_text("用户叫小明，喜欢咖啡", "user-1")
    assert decrypt_memory_text(blob, "user-1", ver) == "用户叫小明，喜欢咖啡"


def test_wrong_user_rejected():
    blob, ver = encrypt_memory_text("秘密", "user-1")
    with pytest.raises(MemoryDecryptError):
        decrypt_memory_text(blob, "user-2", ver)


def test_wrong_key_version_rejected():
    blob, ver = encrypt_memory_text("秘密", "user-1")
    with pytest.raises(MemoryDecryptError):
        decrypt_memory_text(blob, "user-1", ver + 999)


def test_hash_memory_text():
    assert hash_memory_text("a") == hash_memory_text("a")
    assert hash_memory_text("a") != hash_memory_text("b")
