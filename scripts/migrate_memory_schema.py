"""迁移：长期记忆阶段 1 —— 扩展 memories 表 + 新增 memory_profile 表（幂等）.

用法: python scripts/migrate_memory_schema.py
前置: PostgreSQL 已启动（docker compose up -d postgres）
"""
import asyncio

import asyncpg

DB_URL = "postgresql://postgres:postgres@localhost:5432/lumi_db"
LOG_FILE = "migrate_memory_log.txt"


async def main() -> None:
    log = open(LOG_FILE, "w", encoding="utf-8")

    def log_print(msg: str) -> None:
        print(msg)
        log.write(msg + "\n")
        log.flush()

    conn = await asyncpg.connect(DB_URL)
    try:
        log_print("开始迁移长期记忆表结构...")

        log_print("  [1/5] 启用 pgvector 扩展...")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        log_print("  -> OK")

        log_print("  [2/5] memories 表新增字段...")
        memory_columns = [
            "ADD COLUMN IF NOT EXISTS fact_encrypted TEXT",
            "ADD COLUMN IF NOT EXISTS fact_indexable TEXT",
            "ADD COLUMN IF NOT EXISTS memory_type VARCHAR(20) NOT NULL DEFAULT 'experience'",
            "ADD COLUMN IF NOT EXISTS privacy_level SMALLINT NOT NULL DEFAULT 0",
            "ADD COLUMN IF NOT EXISTS embedding vector(512)",
            "ADD COLUMN IF NOT EXISTS confidence FLOAT NOT NULL DEFAULT 1.0",
            "ADD COLUMN IF NOT EXISTS superseded_by UUID",
            "ADD COLUMN IF NOT EXISTS access_count INT NOT NULL DEFAULT 0",
            "ADD COLUMN IF NOT EXISTS key_version INT NOT NULL DEFAULT 1",
        ]
        for col in memory_columns:
            await conn.execute(f"ALTER TABLE memories {col}")
        log_print("  -> OK")

        log_print("  [3/5] 创建 memory_profile 表...")
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_profile (
                user_id    UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                profile    JSONB NOT NULL,
                version    INT NOT NULL DEFAULT 1,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        log_print("  -> OK")

        log_print("  [4/5] memories 检索索引...")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_user_active "
            "ON memories (user_id, is_deleted, expire_at)"
        )
        try:
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_embedding "
                "ON memories USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
            )
            log_print("  -> OK")
        except Exception as exc:  # noqa: BLE001
            log_print(
                f"  -> 向量索引暂跳过（{exc}）；历史数据补齐向量后可用 rebuild_index 类任务重建"
            )

        log_print("  [5/5] 校验...")
        cols = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'memories' ORDER BY ordinal_position"
        )
        names = [r["column_name"] for r in cols]
        expected = [
            "fact_encrypted",
            "fact_indexable",
            "memory_type",
            "privacy_level",
            "embedding",
            "confidence",
            "superseded_by",
            "access_count",
            "key_version",
        ]
        missing = [c for c in expected if c not in names]
        log_print(f"  memories 字段数: {len(names)}，缺失: {missing or '无'}")
        tables = await conn.fetch(
            "SELECT tablename FROM pg_tables "
            "WHERE tablename IN ('memories', 'memory_profile')"
        )
        log_print(f"  表: {[t['tablename'] for t in tables]}")
        log_print("迁移完成!")
    finally:
        await conn.close()
        log.close()


if __name__ == "__main__":
    asyncio.run(main())
