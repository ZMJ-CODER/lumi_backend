"""办公任务的记忆边界。

This service owns conversation summaries, recent-task indexing and the small
amount of presentation preference data that is allowed into office delivery.
The orchestrator keeps compatibility delegates, but no longer owns these
storage details.
"""

from __future__ import annotations

from loguru import logger

from app.agents.orchestration.models import Job
from app.repositories.memory_repository import (
    DefaultMemoryRepository,
    MemoryRepository,
)


class OfficeMemoryService:
    """Office-memory facade; persistence is delegated to ``MemoryRepository``."""

    def __init__(self, repository: MemoryRepository | None = None):
        self._repository = repository or DefaultMemoryRepository()

    async def load_summaries(self, conversation_id: str) -> str:
        return await self._repository.load_summaries(conversation_id)

    async def record_summary(self, job: Job) -> None:
        await self._repository.record_summary(job)

    async def record_task_index(self, job: Job) -> None:
        await self._repository.record_task_index(job)

    async def load_recall_context(
        self, user_id: str, request: str, conversation_id: str | None
    ) -> str:
        return await self._repository.load_recall_context(user_id, request, conversation_id)

    async def load_presentation_preferences(self, user_id: str) -> str:
        return await self._repository.load_presentation_preferences(user_id)

    async def verify_documents(
        self, user_id: str, request: str, office_docs: list[dict] | None
    ) -> list[dict]:
        """Re-resolve client attachments before they reach a planner."""
        if not office_docs:
            return []
        try:
            from app.services.office_docs import ensure_session
        except Exception:  # noqa: BLE001
            return []
        candidates = office_docs
        # When a request names an attachment, verify only that named session.
        # Unmentioned client metadata must not become an implicit authorization
        # request (and this keeps multi-attachment requests deterministic).
        folded_request = str(request or "").casefold()
        named = [
            item for item in office_docs
            if isinstance(item, dict)
            and str(item.get("filename") or "").strip().casefold() in folded_request
        ]
        if named:
            candidates = named
        try:
            from app.agents.orchestration.planner import select_named_office_documents

            selected, _unresolved, has_named = select_named_office_documents(request, office_docs)
            if has_named:
                candidates = selected
        except Exception:  # noqa: BLE001
            pass
        verified: list[dict] = []
        seen: set[str] = set()
        for item in candidates[:12]:
            if not isinstance(item, dict):
                continue
            doc_id = str(item.get("doc_id") or "")[:64]
            if not doc_id or doc_id in seen:
                continue
            try:
                meta = await ensure_session(user_id, doc_id)
            except (LookupError, ValueError):
                logger.info("办公附件未通过会话归属校验: {}", doc_id[:8])
                continue
            seen.add(doc_id)
            verified.append({
                "doc_id": doc_id,
                "filename": str(meta.get("filename") or "")[:500],
                "kind": str(meta.get("kind") or "text")[:20],
            })
        return verified
