"""查询当前 users 表结构和数据分布."""
import asyncio
import asyncpg


async def main():
    conn = await asyncpg.connect("postgresql://postgres:postgres@localhost:5432/lumi_db")

    # 查询列信息
    rows = await conn.fetch(
        """
        SELECT column_name, data_type, character_maximum_length, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_name = 'users'
        ORDER BY ordinal_position
        """
    )
    print("=== 当前 users 表列结构 ===")
    for r in rows:
        print(
            f"{r['column_name']:20s} | {r['data_type']:30s} | "
            f"max_len={r['character_maximum_length']} | "
            f"nullable={r['is_nullable']} | default={r['column_default']}"
        )

    # 查询约束
    constraints = await conn.fetch(
        """
        SELECT conname, contype, pg_get_constraintdef(oid) as def
        FROM pg_constraint
        WHERE conrelid = 'users'::regclass
        """
    )
    print()
    print("=== 当前 users 表约束 ===")
    for c in constraints:
        print(f"{c['conname']:30s} | type={c['contype']} | {c['def']}")

    # 查询索引
    indexes = await conn.fetch(
        """
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE tablename = 'users'
        """
    )
    print()
    print("=== 当前 users 表索引 ===")
    for idx in indexes:
        print(f"{idx['indexname']:30s} | {idx['indexdef']}")

    # 查询现有数据分布
    print()
    print("=== 现有 role 值分布 ===")
    role_rows = await conn.fetch("SELECT role, count(*) as cnt FROM users GROUP BY role")
    for r in role_rows:
        print(f"  role={r['role']}, count={r['cnt']}")

    print()
    print("=== 现有 status 值分布 ===")
    status_rows = await conn.fetch("SELECT status, count(*) as cnt FROM users GROUP BY status")
    for r in status_rows:
        print(f"  status={r['status']}, count={r['cnt']}")

    print()
    null_updated = await conn.fetchval("SELECT count(*) FROM users WHERE updated_at IS NULL")
    print(f"=== updated_at 为 NULL 的行数: {null_updated} ===")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())