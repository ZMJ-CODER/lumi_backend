import asyncio
from app.core.database import engine
from sqlalchemy import text

async def main():
    async with engine.connect() as c:
        r = await c.execute(text("SELECT count(*) FROM users"))
        print("users count:", r.scalar())
        r2 = await c.execute(text("SELECT id, username, role, created_at FROM users"))
        for row in r2:
            print(dict(row._mapping))

asyncio.run(main())
