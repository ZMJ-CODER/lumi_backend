"""长期记忆主密钥轮换：用新密钥重加密全部 L1 密文并升级 key_version.

用法（先备份数据库，再执行）：
  $env:NEW_MEMORY_ENCRYPTION_KEY="<base64 32字节新密钥>"
  $env:NEW_MEMORY_ENCRYPTION_KEY_VERSION="2"        # 可选，默认 当前版本+1
  python scripts/rotate_memory_key.py

流程：
  1. 用当前密钥（settings.MEMORY_ENCRYPTION_KEY）逐条解密 fact_encrypted；
  2. 用新密钥 + 新版本重加密并写回，key_version 同步升级；
  3. 完成后把 .env 的 MEMORY_ENCRYPTION_KEY 与 KEY_VERSION 更新为新值再重启服务。
"""

import asyncio
import base64
import os
import secrets

from loguru import logger
from sqlalchemy import select, update

from app.core.config import settings
from app.core.database import async_session_factory
from app.models.db_models import Memory


def _valid_b64_32(text: str) -> bool:
    try:
        return len(base64.b64decode(text)) == 32
    except Exception:  # noqa: BLE001
        return False


async def main() -> None:
    new_key = os.environ.get("NEW_MEMORY_ENCRYPTION_KEY", "").strip()
    if not new_key or not _valid_b64_32(new_key):
        raise SystemExit("缺少合法的 NEW_MEMORY_ENCRYPTION_KEY（base64 32 字节）")
    new_version = int(os.environ.get("NEW_MEMORY_ENCRYPTION_KEY_VERSION", "").strip() or 0)
    if new_version <= 0:
        new_version = settings.MEMORY_ENCRYPTION_KEY_VERSION + 1

    from app.core.crypto import decrypt_memory_text, encrypt_memory_text

    old_key = settings.MEMORY_ENCRYPTION_KEY
    if not old_key:
        raise SystemExit("当前 MEMORY_ENCRYPTION_KEY 未配置")

    updated = 0
    failed = 0
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(Memory).where(Memory.fact_encrypted.isnot(None))
            )
        ).scalars().all()
        for m in rows:
            try:
                plaintext = decrypt_memory_text(
                    m.fact_encrypted, str(m.user_id), m.key_version or 1
                )
                # 临时切到新密钥 + 新版本重加密
                settings.MEMORY_ENCRYPTION_KEY = new_key
                settings.MEMORY_ENCRYPTION_KEY_VERSION = new_version
                blob, _ = encrypt_memory_text(plaintext, str(m.user_id))
                # 恢复旧密钥（下一轮解密仍用旧）
                settings.MEMORY_ENCRYPTION_KEY = old_key
                await session.execute(
                    update(Memory)
                    .where(Memory.id == m.id)
                    .values(fact_encrypted=blob, key_version=new_version)
                )
                updated += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.warning("记忆 {} 轮换失败: {}", m.id, exc)
                settings.MEMORY_ENCRYPTION_KEY = old_key
        await session.commit()

    print(
        f"轮换完成：共 {len(rows)} 条密文，成功 {updated}，失败 {failed}；"
        f"新版本 {new_version}。请将 .env 的 MEMORY_ENCRYPTION_KEY / "
        f"MEMORY_ENCRYPTION_KEY_VERSION 更新后重启服务。"
    )


if __name__ == "__main__":
    asyncio.run(main())
