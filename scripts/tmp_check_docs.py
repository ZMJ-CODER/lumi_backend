import asyncio, sys
sys.path.insert(0, r'E:\pythonpycharm\lumi_backend')
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool
from sqlalchemy import text
from app.core.config import settings

async def main():
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        rows = (await session.execute(text("SELECT id, filename, space_id, category FROM documents ORDER BY created_at DESC LIMIT 15"))).mappings().all()
        for r in rows:
            print(str(r["id"])[:8], "|", r["filename"], "| space", str(r["space_id"])[:8], "|", r["category"])
    await engine.dispose()
asyncio.run(main())
