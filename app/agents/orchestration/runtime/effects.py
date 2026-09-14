"""持久化外部副作用日志的应用适配器。

The orchestration kernel owns record state transitions. This adapter owns the
Postgres persistence choice. There is intentionally no Redis or process-memory
fallback: when a write cannot reserve durable intent, its tool body must not
run.
"""

from __future__ import annotations

from typing import Any

from lumi_orch.effects import (
    confirm_record,
    effect_intent_for_node as _kernel_effect_intent_for_node,
    intent_record,
    uncertain_record,
)
from lumi_execution import EffectGuard

from app.repositories.effect_journal_repository import (
    EffectJournalRepository,
    EffectJournalUnavailable,
    PostgresEffectJournalRepository,
)


_repository: EffectJournalRepository | None = None


class ApplicationEffectJournal:
    """Adapt the durable repository to the execution package port."""

    async def reserve(self, key: str, intent: dict[str, Any] | None = None):
        return await record_effect_intent(key, intent)

    async def confirm(self, key: str, result: dict[str, Any] | None = None) -> None:
        await confirm_effect(key, result)

    async def mark_uncertain(self, key: str, reason: str = "execution_interrupted") -> None:
        await mark_effect_uncertain(key, reason)

    async def abandon_pending(self, key: str) -> None:
        await abandon_pending_effect(key)


effect_guard = EffectGuard(ApplicationEffectJournal())


def effect_intent_for_node(*, job_id: str, node: Any, tool: str = "") -> dict[str, str]:
    """Expose the kernel intent fingerprint through the app adapter."""
    return _kernel_effect_intent_for_node(job_id=job_id, node=node, tool=tool)


def _journal() -> EffectJournalRepository:
    global _repository
    if _repository is None:
        _repository = PostgresEffectJournalRepository()
    return _repository


def set_effect_journal_repository_for_tests(repository: EffectJournalRepository | None) -> None:
    """Install an explicit test double; production never calls this function."""
    global _repository
    _repository = repository


async def get_effect(key: str) -> dict[str, Any] | None:
    return await _journal().get(key)


async def record_effect_intent(
    key: str,
    intent: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    """Durably reserve an effect before entering its tool body."""
    return await _journal().record_intent(key, intent_record(intent).model_dump(exclude_none=True))


async def confirm_effect(key: str, result: dict[str, Any] | None = None) -> None:
    previous = await get_effect(key)
    if previous is None:
        raise EffectJournalUnavailable("副作用日志记录不存在")
    await _journal().confirm(key, confirm_record(previous, result).model_dump(exclude_none=True))


async def mark_effect_uncertain(key: str, reason: str = "execution_interrupted") -> None:
    previous = await get_effect(key)
    if previous is None:
        raise EffectJournalUnavailable("副作用日志记录不存在")
    await _journal().mark_uncertain(key, uncertain_record(previous, reason).model_dump(exclude_none=True))


# Compatibility shims for independently deployed Temporal workers.
async def begin_effect(key: str, intent: dict[str, Any] | None = None) -> tuple[bool, dict[str, Any] | None]:
    return await record_effect_intent(key, intent)


async def finish_effect(key: str, status: str, result: dict[str, Any] | None = None) -> None:
    if status in {"committed", "confirmed"}:
        await confirm_effect(key, result)
        return
    await mark_effect_uncertain(key, status)


async def abandon_pending_effect(key: str) -> None:
    """Remove an intent known to have stopped before its tool body started."""
    await _journal().abandon_pending(key)


async def recover_orphaned_effect_intents(older_than_seconds: int) -> int:
    """Mark stale reservation-only records uncertain after a safe grace period."""
    return await _journal().mark_stale_intents_uncertain(older_than_seconds)
