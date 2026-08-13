"""迁移：嵌入模型 bge-small-zh(512维) → bge-m3(1024维) + 新增 code_embeddings 表.

步骤：
  1. 删除 document_chunks / memories 上的 ivfflat 向量索引
  2. 清空旧向量（512 维数据无法转换，需重嵌入）
  3. 向量列改为 vector(1024) 并重建索引
  4. 新建 code_embeddings（本地代码向量，file_key 混淆元数据）

注意：执行后需重嵌入已有文档与记忆（scripts/reembed_vectors.py）。

用法（读 DATABASE_URL）:
  docker compose exec api python scripts/migrate_bge_m3.py
"""

import asyncio
import os

import asyncpg

DB_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/lumi_db")


def _to_sync_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql://" + url[len("postgresql+asyncpg://"):]
    return url


async def main() -> None:
    conn = await asyncpg.connect(_to_sync_url(DB_URL))
    try:
        # 1. 删旧向量索引（维度变更前必须删）
        await conn.execute("DROP INDEX IF EXISTS idx_chunks_embedding")
        await conn.execute("DROP INDEX IF EXISTS idx_memories_embedding")
        # 2. 清空旧向量，避免 512→1024 转换失败
        await conn.execute("UPDATE document_chunks SET embedding = NULL")
        await conn.execute("UPDATE memories SET embedding = NULL")
        # 3. 改维度 + 重建索引
        await conn.execute(
            "ALTER TABLE document_chunks ALTER COLUMN embedding TYPE vector(1024)"
        )
        await conn.execute(
            "ALTER TABLE memories ALTER COLUMN embedding TYPE vector(1024)"
        )
        await conn.execute(
            "CREATE INDEX idx_chunks_embedding ON document_chunks "
            "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
        )
        await conn.execute(
            "CREATE INDEX idx_memories_embedding ON memories "
            "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
        )
        # 4. code_embeddings：本地代码向量（file_key 为路径哈希，服务器不知真实路径/代码）
        await conn.execute(
            "ALTER TABLE projects ADD COLUMN IF NOT EXISTS vector_enabled BOOLEAN NOT NULL DEFAULT true"
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS code_embeddings (
                id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                project_id    UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                file_key      VARCHAR(64) NOT NULL,
                function_name VARCHAR(200),
                line_start    INT,
                line_end      INT,
                summary       VARCHAR(1000),
                embedding     vector(1024) NOT NULL,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_code_emb_project ON code_embeddings(project_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_code_emb_embedding ON code_embeddings "
            "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
        )
        cols = await conn.fetch(
            """
            SELECT c.table_name, c.column_name, c.data_type
            FROM information_schema.columns c
            WHERE c.table_name IN ('document_chunks', 'memories', 'code_embeddings')
              AND c.column_name = 'embedding'
            """
        )
        print("向量列现状:", [(r["table_name"], r["data_type"]) for r in cols])
        print("迁移完成。注意：旧向量已清空，需运行 scripts/reembed_vectors.py 重嵌入文档与记忆")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
