"""迁移：新增 projects / project_index 表（本地项目结构索引）.

用法（读 DATABASE_URL 环境变量，容器内直接执行）:
  docker compose exec api python scripts/migrate_projects.py
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
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name       VARCHAR(200) NOT NULL,
                root_label VARCHAR(500),
                file_count INT NOT NULL DEFAULT 0,
                total_size INT NOT NULL DEFAULT 0,
                status     VARCHAR(20) NOT NULL DEFAULT 'ready',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id)")
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS project_index (
                id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                file_path  VARCHAR(1000) NOT NULL,
                symbols    TEXT,
                summary    TEXT,
                file_size  INT NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_project_index_project ON project_index(project_id)"
        )
        tables = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE tablename IN ('projects', 'project_index')"
        )
        assert len(tables) == 2, f"建表不完整: {[t['tablename'] for t in tables]}"
        print("迁移完成: projects / project_index 已就绪")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
