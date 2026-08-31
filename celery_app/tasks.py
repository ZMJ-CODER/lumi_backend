"""Celery 异步任务定义.

任务类型:
  - process_document:   文档分块 → 向量化 → 入库
  - extract_memories:   从对话中提取长期记忆
  - rebuild_index:      重建向量索引
  - cleanup_vectors:    清理冗余向量
  - delete_user_data:   物理清理用户数据
"""

import asyncio
import uuid
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from celery_app import celery_app
from app.core.config import settings
from app.models.db_models import Memory
from app.services.rag.cleaner import DocumentQualityError
from app.services.rag.knowledge import (
    mark_document_retryable,
    process_document_pipeline,
    recover_stale_document_jobs,
)
from app.services import conversation_trim
from app.services.memory.extraction import extract_memories_from_dialog
from app.services.memory.profile import build_user_profile as build_user_profile_record
from app.services.conversation_memory import maintain_conversation_memory
from app.services.usage import aggregate_daily_stats


def _new_async_session() -> tuple[object, async_sessionmaker]:
    """为任务创建独立的异步引擎（NullPool），避免跨事件循环复用连接."""
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, factory


@celery_app.task(bind=True, max_retries=3)
def process_document(
    self,
    document_id: str,
    file_path: str,
    user_id: str,
    space_id: str,
    category: str | None = None,
):
    """处理上传的文档：解析 → 清洗 → 分类 → 分块 → 嵌入 → 存入 pgvector."""
    async def _run() -> int:
        engine, factory = _new_async_session()
        try:
            async with factory() as session:
                return await process_document_pipeline(
                    session,
                    document_id,
                    file_path,
                    user_category=category,
                    celery_task_id=str(self.request.id or ""),
                )
        finally:
            await engine.dispose()

    try:
        chunk_count = asyncio.run(_run())
        logger.debug("[Task] process_document 完成: doc={} chunks={}", document_id, chunk_count)
    except DocumentQualityError as exc:
        # 质量不达标：状态已在管线中标记为 error，属最终结果，不重试
        logger.warning("[Task] process_document 质量不达标，跳过重试: {}", exc)
    except Exception as exc:
        logger.error("[Task] process_document 失败: doc={} err={}", document_id, exc)
        # Celery 已耗尽重试时，管线写入的 error 是最终状态；不能再置为
        # pending，否则 watchdog 会把确定性错误当作 worker 丢失重新投递。
        if self.request.retries >= self.max_retries:
            raise
        async def _release() -> None:
            engine, factory = _new_async_session()
            try:
                async with factory() as session:
                    await mark_document_retryable(session, document_id)
            finally:
                await engine.dispose()
        asyncio.run(_release())
        raise self.retry(exc=exc, countdown=5) from exc


@celery_app.task(bind=True, max_retries=1)
def recover_stale_documents(self):
    """Requeue documents abandoned by a killed/evicted Celery worker."""
    async def _run() -> list[dict]:
        engine, factory = _new_async_session()
        try:
            async with factory() as session:
                return await recover_stale_document_jobs(
                    session, settings.CELERY_DOCUMENT_STALE_AFTER_SECONDS
                )
        finally:
            await engine.dispose()

    try:
        documents = asyncio.run(_run())
        for document in documents:
            process_document.apply_async(
                args=(
                    document["document_id"],
                    document["file_path"],
                    document["user_id"],
                    document["space_id"],
                    document["category"],
                )
            )
        if documents:
            logger.warning("[Task] 恢复 {} 个超时文档任务", len(documents))
        return len(documents)
    except Exception as exc:  # noqa: BLE001
        logger.error("[Task] recover_stale_documents 失败: {}", exc)
        raise self.retry(exc=exc, countdown=60) from exc


@celery_app.task(bind=True, max_retries=3)
def extract_memories(self, user_id: str, conversation_id: str, messages: list[dict]):
    """从一段对话中批量提取长期记忆关键事实（消息按时间顺序）."""
    async def _run() -> int:
        engine, factory = _new_async_session()
        try:
            async with factory() as session:
                return await extract_memories_from_dialog(
                    session, user_id, conversation_id, messages
                )
        finally:
            await engine.dispose()

    try:
        count = asyncio.run(_run())
        logger.debug("[Task] extract_memories 完成: user={} conv={} new={}", user_id, conversation_id, count)
    except Exception as exc:
        logger.error("[Task] extract_memories 失败: user={} conv={} err={}", user_id, conversation_id, exc)
        raise self.retry(exc=exc, countdown=10) from exc


@celery_app.task(bind=True, max_retries=3)
def maintain_conversation_memory_task(self, user_id: str, conversation_id: str):
    """异步维护摘要，并在安全前提下执行 token 滑动淘汰。"""
    async def _run() -> int:
        engine, factory = _new_async_session()
        try:
            async with factory() as session:
                created = await maintain_conversation_memory(session, user_id, conversation_id)
                # 只有完整段摘要已提交后才尝试删除旧原文，确保待淘汰前缀已被 L1 覆盖。
                if created:
                    await conversation_trim.trim_conversation_messages(session, conversation_id)
                return created
        finally:
            await engine.dispose()

    try:
        count = asyncio.run(_run())
        logger.debug("[Task] maintain_conversation_memory 完成: conv={} segments={}", conversation_id, count)
    except Exception as exc:
        logger.error("[Task] maintain_conversation_memory 失败: conv={} err={}", conversation_id, exc)
        raise self.retry(exc=exc, countdown=10) from exc


@celery_app.task(bind=True, max_retries=3)
def build_user_profile(self, user_id: str):
    """聚合用户活跃事实生成/刷新画像（memory_profile）."""
    async def _run() -> str | None:
        engine, factory = _new_async_session()
        try:
            async with factory() as session:
                profile = await build_user_profile_record(session, user_id)
                return str(profile.version) if profile else None
        finally:
            await engine.dispose()

    try:
        version = asyncio.run(_run())
        logger.debug("[Task] build_user_profile 完成: user={} version={}", user_id, version)
    except Exception as exc:
        logger.error("[Task] build_user_profile 失败: user={} err={}", user_id, exc)
        raise self.retry(exc=exc, countdown=10) from exc


@celery_app.task(bind=True, max_retries=2)
def build_all_user_profiles(self):
    """为所有注册用户入队画像重建（每日定时）."""
    async def _run() -> list[str]:
        engine, factory = _new_async_session()
        try:
            async with factory() as session:
                from app.models.db_models import User

                uids = (await session.execute(select(User.id))).scalars().all()
                return [str(u) for u in uids]
        finally:
            await engine.dispose()

    try:
        uids = asyncio.run(_run())
        for uid in uids:
            build_user_profile.delay(uid)
        logger.debug("[Task] build_all_user_profiles 入队: users={}", len(uids))
    except Exception as exc:  # noqa: BLE001
        logger.error("[Task] build_all_user_profiles 失败: {}", exc)
        raise self.retry(exc=exc, countdown=60) from exc


@celery_app.task(bind=True)
def touch_memories(self, memory_ids: list[str]):
    """记忆强化：被召回后 access_count+1、更新 last_accessed."""
    async def _run() -> None:
        engine, factory = _new_async_session()
        try:
            async with factory() as session:
                ids = [uuid.UUID(str(i)) for i in memory_ids]
                if not ids:
                    return
                await session.execute(
                    update(Memory)
                    .where(Memory.id.in_(ids))
                    .values(
                        access_count=Memory.access_count + 1,
                        last_accessed=datetime.now(timezone.utc),
                    )
                )
                await session.commit()
        finally:
            await engine.dispose()

    try:
        asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        logger.error("[Task] touch_memories 失败: {}", exc)


@celery_app.task(bind=True)
def cleanup_memories(self):
    """清理长期记忆：过期低重要度物理删除；superseded 物理删除；活跃记忆按访问次数微调重要度."""
    async def _run() -> None:
        engine, factory = _new_async_session()
        try:
            async with factory() as session:
                now = datetime.now(timezone.utc)
                expired = await session.execute(
                    delete(Memory).where(
                        Memory.expire_at.isnot(None),
                        Memory.expire_at < now,
                        Memory.importance < settings.MEMORY_CLEANUP_THRESHOLD,
                    )
                )
                superseded = await session.execute(
                    delete(Memory).where(
                        Memory.is_deleted.is_(True),
                        Memory.superseded_by.isnot(None),
                    )
                )
                # 对抗遗忘：被反复使用的记忆重要度缓慢上升（上限 0.95）
                await session.execute(
                    update(Memory)
                    .where(Memory.access_count >= 10, Memory.importance < 0.95)
                    .values(importance=func.least(Memory.importance + 0.02, 0.95))
                )
                await session.commit()
                logger.debug(
                    "[Task] cleanup_memories 完成: expired={} superseded={}",
                    expired.rowcount,
                    superseded.rowcount,
                )
        finally:
            await engine.dispose()

    try:
        asyncio.run(_run())
    except Exception as exc:
        logger.error("[Task] cleanup_memories 失败: {}", exc)
        raise


@celery_app.task(bind=True)
def aggregate_token_stats(self):
    """每日聚合 LLM token 用量：llm_usage → daily_token_stats（用户×日期×用途×模型），并清理原始明细."""
    async def _run() -> int:
        engine, factory = _new_async_session()
        try:
            async with factory() as session:
                return await aggregate_daily_stats(session)
        finally:
            await engine.dispose()

    try:
        groups = asyncio.run(_run())
        logger.debug("[Task] aggregate_token_stats 完成: groups={}", groups)
    except Exception as exc:  # noqa: BLE001
        logger.error("[Task] aggregate_token_stats 失败: {}", exc)
        raise


@celery_app.task(bind=True, max_retries=3)
def trim_conversation_messages(self, conversation_id: str):
    """按 token 滑动窗口物理淘汰已摘要的最旧消息及附件。"""
    async def _run() -> int:
        engine, factory = _new_async_session()
        try:
            async with factory() as session:
                return await conversation_trim.trim_conversation_messages(session, conversation_id)
        finally:
            await engine.dispose()

    try:
        removed = asyncio.run(_run())
        logger.debug("[Task] trim_conversation_messages 完成: conv={} removed={}", conversation_id, removed)
    except Exception as exc:  # noqa: BLE001
        logger.error("[Task] trim_conversation_messages 失败: conv={} err={}", conversation_id, exc)
        raise self.retry(exc=exc, countdown=10) from exc


@celery_app.task(bind=True)
def cleanup_conversations(self):
    """每日兜底：扫描普通会话，补偿失败重试后的 token 窗口淘汰。"""
    async def _run() -> int:
        engine, factory = _new_async_session()
        try:
            async with factory() as session:
                return await conversation_trim.cleanup_all_conversations(session)
        finally:
            await engine.dispose()

    try:
        trimmed = asyncio.run(_run())
        logger.debug("[Task] cleanup_conversations 完成: removed={}", trimmed)
    except Exception as exc:  # noqa: BLE001
        logger.error("[Task] cleanup_conversations 失败: {}", exc)
        raise


@celery_app.task(bind=True)
def cleanup_generated_files(self):
    """定时清理后端生成的临时/产物文件：
    - 过期办公文档会话（data/office）
    - 通用脚本产物目录（data/uploads/office_outputs，超过 TTL 删除）
    - 沙箱残留临时目录（系统 temp 下的 lumi_sandbox_*，崩溃遗留兜底）
    """

    async def _run() -> None:
        from app.services.office_docs import cleanup_expired_sessions, cleanup_generic_outputs

        expired_sessions = await cleanup_expired_sessions()
        removed_outputs = cleanup_generic_outputs(settings.GENERATED_FILES_TTL_DAYS)

        # 沙箱残留临时目录（正常路径即时清理，这里兜底崩溃遗留）
        import shutil
        import tempfile
        import time
        from pathlib import Path

        base = Path(tempfile.gettempdir())
        cutoff = time.time() - settings.SANDBOX_TEMP_TTL_HOURS * 3600
        removed_sandbox = 0
        for entry in base.glob("lumi_sandbox_*"):
            try:
                if entry.is_dir() and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed_sandbox += 1
            except OSError:
                continue
        logger.info(
            "[Task] cleanup_generated_files: 过期会话={} 产物目录={} 沙箱临时={}",
            expired_sessions,
            removed_outputs,
            removed_sandbox,
        )

    try:
        asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        logger.error("[Task] cleanup_generated_files 失败: {}", exc)


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
        logger.debug("[Task] rebuild_index 完成: space={}", space_id)
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
                logger.debug("[Task] cleanup_vectors 清理 {} 条冗余向量", result.rowcount)
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
                    "DELETE FROM memory_profile WHERE user_id = :uid",
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
                logger.debug("[Task] delete_user_data 完成: user={}", user_id)
        finally:
            await engine.dispose()

    try:
        asyncio.run(_run())
    except Exception as exc:
        logger.error("[Task] delete_user_data 失败: {}", exc)
        raise
