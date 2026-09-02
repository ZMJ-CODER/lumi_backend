"""办公会话记忆的持久化边界。"""

from __future__ import annotations

from typing import Protocol

from loguru import logger

from app.agents.orchestration.models import Job, JobStatus


class MemoryRepository(Protocol):
    async def load_summaries(self, conversation_id: str) -> str: ...

    async def record_summary(self, job: Job) -> None: ...

    async def record_task_index(self, job: Job) -> None: ...

    async def load_recall_context(
        self, user_id: str, request: str, conversation_id: str | None
    ) -> str: ...

    async def load_presentation_preferences(self, user_id: str) -> str: ...


class DefaultMemoryRepository:
    """Redis + SQLAlchemy implementation of office memory storage.

    Imports are intentionally local: importing orchestration/planner modules
    must not eagerly initialize Redis or a database connection.
    """

    summary_key = "conv:office:sum:{conversation_id}"
    summary_recorded_key = "conv:office:summed:{job_id}"
    summary_max = 8

    async def load_summaries(self, conversation_id: str) -> str:
        if not conversation_id:
            return ""
        try:
            from app.core.redis import get_redis

            items = await get_redis().lrange(
                self.summary_key.format(conversation_id=conversation_id), 0, -1
            )
            if not items:
                return ""
            lines = [
                f"{idx}. {str(item)[:300]}"
                for idx, item in enumerate(reversed(items), 1)
            ]
            return "\n".join(lines)[:3000]
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取办公任务摘要失败: {}", exc)
            return ""

    async def record_summary(self, job: Job) -> None:
        if not job or not job.conversation_id or job.status not in {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
        }:
            return
        try:
            from app.core.redis import get_redis

            redis = get_redis()
            recorded_key = self.summary_recorded_key.format(job_id=job.job_id)
            if await redis.exists(recorded_key):
                return
            result = job.result or {}
            final = str(result.get("final_answer") or result.get("answer") or "")
            summary = (
                f"任务：{job.request[:120]}"
                + (f" | 计划：{str(job.plan_text or '')[:150]}" if job.plan_text else "")
                + (f" | 结果：{final[:250]}" if final else "")
                + (f" | 失败：{str(job.error)[:100]}" if job.error else "")
            )
            key = self.summary_key.format(conversation_id=job.conversation_id)
            await redis.rpush(key, summary[:600])
            await redis.ltrim(key, -self.summary_max, -1)
            await redis.setex(recorded_key, 86400 * 7, "1")
        except Exception as exc:  # noqa: BLE001
            logger.debug("写入办公任务摘要失败: {}", exc)

    async def record_task_index(self, job: Job) -> None:
        if job.scene != "office" or job.status not in {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
        }:
            return
        try:
            from app.core.database import async_session_factory
            from app.services.office_task_memory import upsert_office_task_index

            async with async_session_factory() as session:
                await upsert_office_task_index(session, job)
        except Exception as exc:  # noqa: BLE001
            logger.debug("写入办公近期任务索引失败 {}: {}", job.job_id[:8], exc)

    async def load_recall_context(
        self, user_id: str, request: str, conversation_id: str | None
    ) -> str:
        try:
            from app.services.office_task_memory import (
                needs_office_task_recall,
                recall_office_tasks,
            )

            if not needs_office_task_recall(request):
                return ""
            from app.core.database import async_session_factory

            async with async_session_factory() as session:
                recalled = await recall_office_tasks(
                    session,
                    user_id=user_id,
                    request=request,
                    conversation_id=conversation_id,
                )
            return recalled.context
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取办公近期任务索引失败: {}", exc)
            return ""

    async def load_presentation_preferences(self, user_id: str) -> str:
        try:
            from app.core.database import async_session_factory
            from app.services.office_task_memory import get_office_presentation_preferences

            async with async_session_factory() as session:
                return await get_office_presentation_preferences(session, user_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取办公展示偏好失败: {}", exc)
            return ""
