"""迁移：users 表新增 prompt_id 列（角色提示词选择，可空，幂等）.

用法: python scripts/migrate_prompt_schema.py
"""
import asyncio

import asyncpg

DB_URL = "postgresql://postgres:postgres@localhost:5432/lumi_db"


async def main() -> None:
    conn = await asyncpg.connect(DB_URL)
    try:
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS prompt_id VARCHAR(50)")
        cols = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'users' AND column_name = 'prompt_id'"
        )
        assert cols, "prompt_id 列未创建成功"
        print("迁移完成: users.prompt_id 已就绪")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
