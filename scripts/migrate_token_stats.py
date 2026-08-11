"""迁移：新增 llm_usage / daily_token_stats 表（LLM token 用量统计，幂等）.

用法: python scripts/migrate_token_stats.py
"""
import asyncio

import asyncpg

DB_URL = "postgresql://postgres:postgres@localhost:5432/lumi_db"


async def main() -> None:
    conn = await asyncpg.connect(DB_URL)
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_usage (
                id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id          UUID,
                category         VARCHAR(40) NOT NULL,
                model            VARCHAR(100) NOT NULL DEFAULT '',
                prompt_tokens    INT NOT NULL DEFAULT 0,
                completion_tokens INT NOT NULL DEFAULT 0,
                created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_usage_user_cat_created "
            "ON llm_usage(user_id, category, created_at)"
        )
        await conn.execute(
            """
            DROP TABLE IF EXISTS daily_token_stats;
            CREATE TABLE IF NOT EXISTS daily_token_stats (
                id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id           UUID,
                stat_date         DATE NOT NULL,
                category          VARCHAR(40) NOT NULL,
                model             VARCHAR(100) NOT NULL DEFAULT '',
                prompt_tokens     INT NOT NULL DEFAULT 0,
                completion_tokens INT NOT NULL DEFAULT 0,
                call_count        INT NOT NULL DEFAULT 0,
                CONSTRAINT uq_daily_token_stats UNIQUE (user_id, stat_date, category, model)
            )
            """
        )
        tables = await conn.fetch(
            "SELECT tablename FROM pg_tables "
            "WHERE tablename IN ('llm_usage', 'daily_token_stats')"
        )
        assert len(tables) == 2, f"建表不完整: {[t['tablename'] for t in tables]}"
        print("迁移完成: llm_usage / daily_token_stats 已就绪")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
