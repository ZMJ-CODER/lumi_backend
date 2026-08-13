"""迁移 bge-m3 后：重嵌入已有文档（status=ready 的文档重新跑处理管线）.

记忆向量（memories.embedding）需要按隐私规则单独重建，本脚本暂不处理，
会在日志中提示。用法（读 DATABASE_URL）:
  docker compose exec api python scripts/reembed_vectors.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.core.database import async_session_factory
from app.models.db_models import Document
from app.services.rag.knowledge import process_document_pipeline


async def main() -> None:
    async with async_session_factory() as session:
        docs = (
            await session.execute(
                select(Document).where(Document.status == "ready").order_by(Document.created_at)
            )
        ).scalars().all()
        print(f"待重嵌入文档: {len(docs)} 篇")
        ok = 0
        for doc in docs:
            try:
                count = await process_document_pipeline(
                    session, str(doc.id), str(doc.file_path), user_category=doc.category
                )
                ok += 1
                print(f"  重嵌入 {doc.filename}: {count} 块")
            except Exception as exc:  # noqa: BLE001
                print(f"  失败 {doc.filename}: {exc}")
        await session.commit()
    print(f"完成：{ok}/{len(docs)} 篇。记忆向量（memories）需单独重建。")


if __name__ == "__main__":
    asyncio.run(main())
