"""数据库初始化脚本 —— 创建 lumi_db 数据库并运行 Alembic schema 迁移.

使用方式:
    python scripts/init_db.py

功能:
  1. 连接 postgres 默认库 → 创建 lumi_db（如不存在）
  2. 运行 ``alembic upgrade head`` 创建表、扩展和索引
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.models.db_base import Base
from app.models import db_models  # noqa: F401


async def create_database_if_not_exists() -> None:
    """连接默认 postgres 库，创建 lumi_db 数据库."""
    admin_engine = create_async_engine(
        settings.DATABASE_ADMIN_URL,
        isolation_level="AUTOCOMMIT",
    )
    try:
        async with admin_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = 'lumi_db'")
            )
            if result.fetchone():
                print("[OK] 数据库 lumi_db 已存在，跳过创建")
            else:
                await conn.execute(text("CREATE DATABASE lumi_db"))
                print("[OK] 数据库 lumi_db 创建成功")
    finally:
        await admin_engine.dispose()


def upgrade_schema() -> None:
    """Apply the versioned schema contract in the Alembic migration chain."""
    from alembic import command
    from alembic.config import Config

    config = Config(os.path.join(os.path.dirname(os.path.dirname(__file__)), "alembic.ini"))
    command.upgrade(config, "head")
    print("[OK] Alembic 已升级到 head")


async def verify_tables() -> None:
    """验证表结构."""
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with engine.connect() as conn:
            for table_name in sorted(Base.metadata.tables.keys()):
                result = await conn.execute(
                    text(
                        "SELECT column_name, data_type, udt_name "
                        "FROM information_schema.columns "
                        "WHERE table_name = :t ORDER BY ordinal_position"
                    ),
                    {"t": table_name},
                )
                cols = result.fetchall()
                print(f"\n  [{table_name}]")
                for col in cols:
                    dtype = col[2] if col[1] in ("USER-DEFINED", "ARRAY") else col[1]
                    print(f"    {col[0]:28s} {dtype}")
    finally:
        await engine.dispose()


async def main() -> None:
    print("=" * 55)
    print("  Lumi Backend - 数据库初始化 (pgvector)")
    print("=" * 55)
    print(f"  目标库:     lumi_db")
    print(f"  主机:       localhost:5432")
    print(f"  用户:       postgres")
    print(f"  向量维度:   {settings.EMBEDDING_DIMENSION}")
    print("=" * 55)
    print()

    await create_database_if_not_exists()
    await asyncio.to_thread(upgrade_schema)
    await verify_tables()

    print()
    print("=" * 55)
    print("  数据库初始化完成！")
    print("=" * 55)


if __name__ == "__main__":
    asyncio.run(main())
