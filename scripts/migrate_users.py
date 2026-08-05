"""迁移 users 表结构至设计文档 v1.0 目标结构."""
import asyncio
import sys
import asyncpg


async def main():
    log = open("migrate_log.txt", "w", encoding="utf-8")
    
    def log_print(msg):
        print(msg)
        log.write(msg + "\n")
        log.flush()
    
    try:
        log_print("开始迁移 users 表结构...")
        conn = await asyncpg.connect("postgresql://postgres:postgres@localhost:5432/lumi_db")
        log_print("数据库连接成功")

        # 1. id 添加 DEFAULT gen_random_uuid()
        log_print("  [1/5] 设置 id 默认值为 gen_random_uuid()...")
        await conn.execute("ALTER TABLE users ALTER COLUMN id SET DEFAULT gen_random_uuid()")
        log_print("  -> OK")

        # 2. role: 更新 CHECK 约束 + 添加 DEFAULT
        log_print("  [2/5] 更新 role CHECK 约束 (superadmin -> super_admin) 并设置默认值...")
        await conn.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check")
        log_print("  -> 旧约束已删除")
        await conn.execute("UPDATE users SET role = 'super_admin' WHERE role = 'superadmin'")
        log_print("  -> 数据已更新")
        await conn.execute("ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN ('super_admin', 'admin', 'user'))")
        log_print("  -> 新约束已添加")
        await conn.execute("ALTER TABLE users ALTER COLUMN role SET DEFAULT 'user'")
        log_print("  -> 默认值已设置")

        # 3. avatar_url: VARCHAR(500) -> TEXT
        log_print("  [3/5] 将 avatar_url 从 VARCHAR(500) 改为 TEXT...")
        await conn.execute("ALTER TABLE users ALTER COLUMN avatar_url TYPE TEXT")
        log_print("  -> OK")

        # 4. status: 添加 DEFAULT 'active'
        log_print("  [4/5] 设置 status 默认值为 'active'...")
        await conn.execute("ALTER TABLE users ALTER COLUMN status SET DEFAULT 'active'")
        log_print("  -> OK")

        # 5. updated_at: 改为 NOT NULL + DEFAULT now()
        log_print("  [5/5] 将 updated_at 改为 NOT NULL + DEFAULT now()...")
        await conn.execute("UPDATE users SET updated_at = now() WHERE updated_at IS NULL")
        await conn.execute("ALTER TABLE users ALTER COLUMN updated_at SET DEFAULT now()")
        await conn.execute("ALTER TABLE users ALTER COLUMN updated_at SET NOT NULL")
        log_print("  -> OK")

        log_print("")
        log_print("迁移完成! 验证新结构...")

        # 验证新结构
        rows = await conn.fetch(
            "SELECT column_name, data_type, character_maximum_length, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_name = 'users' ORDER BY ordinal_position"
        )
        log_print("")
        log_print("=== 迁移后 users 表列结构 ===")
        for r in rows:
            log_print(f"  {r['column_name']:20s} | {r['data_type']:30s} | max_len={r['character_maximum_length']} | nullable={r['is_nullable']} | default={r['column_default']}")

        # 验证约束
        constraints = await conn.fetch(
            "SELECT conname, contype, pg_get_constraintdef(oid) as def FROM pg_constraint WHERE conrelid = 'users'::regclass ORDER BY conname"
        )
        log_print("")
        log_print("=== 迁移后 users 表约束 ===")
        for c in constraints:
            log_print(f"  {c['conname']:30s} | type={c['contype']} | {c['def']}")

        await conn.close()
        log_print("")
        log_print("所有迁移已成功完成!")
        
    except Exception as e:
        log_print(f"错误: {e}")
        import traceback
        log_print(traceback.format_exc())
        sys.exit(1)
    finally:
        log.close()


if __name__ == "__main__":
    asyncio.run(main())