"""迁移：新增 user_prompts 表（用户自定义角色提示词，幂等）.

用法: python scripts/migrate_user_prompts.py
"""
import asyncio

import asyncpg

DB_URL = "postgresql://postgres:postgres@localhost:5432/lumi_db"


async def main() -> None:
    conn = await asyncpg.connect(DB_URL)
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_prompts (
                id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name        VARCHAR(100) NOT NULL,
                description TEXT,
                content     TEXT NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_prompts_user ON user_prompts(user_id)")
        tables = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE tablename = 'user_prompts'"
        )
        assert tables, "user_prompts 表未创建成功"
        print("迁移完成: user_prompts 已就绪")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
