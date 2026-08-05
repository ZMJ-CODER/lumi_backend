"""数据库初始化脚本 —— 创建 lumi_db 数据库 + 所有表 + pgvector 索引.

使用方式:
    python scripts/init_db.py

功能:
  1. 连接 postgres 默认库 → 创建 lumi_db（如不存在）
  2. 启用 pgvector 扩展
  3. 根据 SQLAlchemy ORM 模型创建所有表
  4. 创建向量索引（ivfflat）
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


async def enable_pgvector() -> None:
    """启用 pgvector 扩展."""
    engine = create_async_engine(settings.DATABASE_URL, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            print("[OK] pgvector 扩展已启用")
    finally:
        await engine.dispose()


async def create_all_tables() -> None:
    """根据 ORM 模型创建所有表."""
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        print("[OK] 所有表创建完成")
        print()
        print("已创建的表:")
        for table_name in sorted(Base.metadata.tables.keys()):
            print(f"  - {table_name}")
    finally:
        await engine.dispose()


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
    await enable_pgvector()
    await create_all_tables()
    await verify_tables()

    print()
    print("=" * 55)
    print("  数据库初始化完成！")
    print("=" * 55)


if __name__ == "__main__":
    asyncio.run(main())
