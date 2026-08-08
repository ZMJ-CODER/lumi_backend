"""Celery 异步任务定义.

任务类型:
  - process_document:   文档分块 → 向量化 → 入库
  - extract_memories:   从对话中提取长期记忆
  - rebuild_index:      重建向量索引
  - cleanup_vectors:    清理冗余向量
  - delete_user_data:   物理清理用户数据
"""

import asyncio

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from celery_app import celery_app
from app.core.config import settings
from app.services.rag.cleaner import DocumentQualityError
from app.services.rag.knowledge import process_document_pipeline


def _new_async_session() -> tuple[object, async_sessionmaker]:
    """为任务创建独立的异步引擎（NullPool），避免跨事件循环复用连接."""
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, factory


@celery_app.task(bind=True, max_retries=3)
def process_document(self, document_id: str, file_path: str, user_id: str, space_id: str):
    """处理上传的文档：分块 → 嵌入 → 存入 pgvector."""
    async def _run() -> int:
        engine, factory = _new_async_session()
        try:
            async with factory() as session:
                return await process_document_pipeline(session, document_id, file_path)
        finally:
            await engine.dispose()

    try:
        chunk_count = asyncio.run(_run())
        logger.info("[Task] process_document 完成: doc={} chunks={}", document_id, chunk_count)
    except DocumentQualityError as exc:
        # 质量不达标：状态已在管线中标记为 error，属最终结果，不重试
        logger.warning("[Task] process_document 质量不达标，跳过重试: {}", exc)
    except Exception as exc:
        logger.error("[Task] process_document 失败: doc={} err={}", document_id, exc)
        raise self.retry(exc=exc, countdown=5)


@celery_app.task(bind=True, max_retries=3)
def extract_memories(self, user_id: str, conversation_id: str, user_msg: str, assistant_msg: str):
    """从对话中提取长期记忆关键事实."""
    # TODO:
    # 1. 调用 LLM 提取关键事实
    # 2. 去重（与已有记忆比较）
    # 3. 写入 memories 表
    # 4. 清除 Redis 记忆缓存
    pass


@celery_app.task(bind=True)
def rebuild_index(self, space_id: str | None = None):
    """重建向量索引."""
    async def _run() -> None:
        engine, _ = _new_async_session()
        try:
            async with engine.connect() as conn:
                await conn.execute(text("DROP INDEX IF EXISTS idx_chunks_embedding"))
                await conn.execute(
                    text(
                        "CREATE INDEX idx_chunks_embedding ON document_chunks "
                        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
                    )
                )
                await conn.commit()
        finally:
            await engine.dispose()

    try:
        asyncio.run(_run())
        logger.info("[Task] rebuild_index 完成: space={}", space_id)
    except Exception as exc:
        logger.error("[Task] rebuild_index 失败: {}", exc)
        raise


@celery_app.task(bind=True)
def cleanup_vectors(self):
    """清理已删除文档的冗余向量."""
    async def _run() -> None:
        engine, _ = _new_async_session()
        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text(
                        "DELETE FROM document_chunks "
                        "WHERE document_id NOT IN (SELECT id FROM documents)"
                    )
                )
                await conn.commit()
                logger.info("[Task] cleanup_vectors 清理 {} 条冗余向量", result.rowcount)
        finally:
            await engine.dispose()

    try:
        asyncio.run(_run())
    except Exception as exc:
        logger.error("[Task] cleanup_vectors 失败: {}", exc)
        raise


@celery_app.task(bind=True)
def delete_user_data(self, user_id: str):
    """物理清理用户所有数据（24h 延迟执行）."""
    async def _run() -> None:
        engine, _ = _new_async_session()
        try:
            async with engine.connect() as conn:
                statements = [
                    "DELETE FROM messages WHERE conversation_id IN "
                    "(SELECT id FROM conversations WHERE user_id = :uid)",
                    "DELETE FROM conversations WHERE user_id = :uid",
                    "DELETE FROM memories WHERE user_id = :uid",
                    "DELETE FROM document_chunks WHERE user_id = :uid",
                    "DELETE FROM documents WHERE user_id = :uid",
                    "DELETE FROM knowledge_spaces WHERE user_id = :uid AND is_public = false",
                    "DELETE FROM control_logs WHERE user_id = :uid",
                    "DELETE FROM refresh_tokens WHERE user_id = :uid",
                    "DELETE FROM users WHERE id = :uid",
                ]
                for sql in statements:
                    await conn.execute(text(sql), {"uid": user_id})
                await conn.commit()
                logger.info("[Task] delete_user_data 完成: user={}", user_id)
        finally:
            await engine.dispose()

    try:
        asyncio.run(_run())
    except Exception as exc:
        logger.error("[Task] delete_user_data 失败: {}", exc)
        raise
