"""外部副作用日志的持久化边界。

副作用日志刻意存放在 PostgreSQL 而非 Redis：Redis 适合协调，但进程崩溃后
丢失 Redis 键绝不能让一个已对外可见的操作再次自动重试。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from sqlalchemy import and_, delete, select, update
from sqlalchemy.dialects.postgresql import insert

from app.models.db_models import EffectJournal


class EffectJournalUnavailable(RuntimeError):
    """Raised when the durable journal cannot make a safety decision."""


class EffectJournalRepository(Protocol):
    async def get(self, key: str) -> dict[str, Any] | None: ...

    async def record_intent(
        self, key: str, record: dict[str, Any]
    ) -> tuple[bool, dict[str, Any] | None]: ...

    async def confirm(self, key: str, record: dict[str, Any]) -> None: ...

    async def mark_uncertain(self, key: str, record: dict[str, Any]) -> None: ...

    async def abandon_pending(self, key: str) -> None: ...

    async def mark_stale_intents_uncertain(self, older_than_seconds: int) -> int: ...


def _as_datetime(timestamp: float | None) -> datetime | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def _as_timestamp(value: datetime | None) -> float | None:
    if value is None:
        return None
    return value.timestamp()


def _model_record(model: EffectJournal) -> dict[str, Any]:
    return {
        "status": model.status,
        "intent": dict(model.intent_payload or {}),
        "intent_at": _as_timestamp(model.intent_at),
        "confirmed_at": _as_timestamp(model.confirmed_at),
        "uncertain_at": _as_timestamp(model.uncertain_at),
        "reason": model.reason,
        "result": dict(model.result_payload) if isinstance(model.result_payload, dict) else None,
        "updated_at": _as_timestamp(model.updated_at) or 0.0,
    }


class PostgresEffectJournalRepository:
    """Postgres implementation. Every mutation is independently committed."""

    async def get(self, key: str) -> dict[str, Any] | None:
        try:
            from app.core.database import async_session_factory

            async with async_session_factory() as session:
                row = await session.scalar(
                    select(EffectJournal).where(EffectJournal.idempotency_key == key)
                )
                return _model_record(row) if row is not None else None
        except Exception as exc:  # noqa: BLE001
            raise EffectJournalUnavailable("副作用日志数据库不可用") from exc

    async def record_intent(
        self, key: str, record: dict[str, Any]
    ) -> tuple[bool, dict[str, Any] | None]:
        try:
            from app.core.database import async_session_factory

            intent = dict(record.get("intent") or {})
            values = {
                "idempotency_key": key,
                "job_id": str(intent.get("job_id") or ""),
                "node_id": str(intent.get("node_id") or ""),
                "tool": str(intent.get("tool") or "")[:160],
                "params_sha256": str(intent.get("params_sha256") or "")[:64],
                "status": "intent",
                "intent_payload": intent,
                "intent_at": _as_datetime(record.get("intent_at")),
            }
            statement = (
                insert(EffectJournal)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[EffectJournal.idempotency_key])
                .returning(EffectJournal.idempotency_key)
            )
            async with async_session_factory() as session:
                async with session.begin():
                    created = (await session.scalar(statement)) is not None
                    existing = None
                    if not created:
                        row = await session.scalar(
                            select(EffectJournal).where(EffectJournal.idempotency_key == key)
                        )
                        existing = _model_record(row) if row is not None else None
            return created, existing
        except Exception as exc:  # noqa: BLE001
            raise EffectJournalUnavailable("副作用日志数据库不可用") from exc

    async def _replace(self, key: str, record: dict[str, Any]) -> None:
        values = {
            "status": str(record["status"]),
            "intent_payload": dict(record.get("intent") or {}),
            "intent_at": _as_datetime(record.get("intent_at")),
            "confirmed_at": _as_datetime(record.get("confirmed_at")),
            "uncertain_at": _as_datetime(record.get("uncertain_at")),
            "reason": record.get("reason"),
            "result_payload": record.get("result"),
            "updated_at": datetime.now(timezone.utc),
        }
        try:
            from app.core.database import async_session_factory

            async with async_session_factory() as session:
                async with session.begin():
                    result = await session.execute(
                        update(EffectJournal)
                        .where(EffectJournal.idempotency_key == key)
                        .values(**values)
                    )
                    if not result.rowcount:
                        raise EffectJournalUnavailable("副作用日志记录不存在")
        except EffectJournalUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise EffectJournalUnavailable("副作用日志数据库不可用") from exc

    async def confirm(self, key: str, record: dict[str, Any]) -> None:
        await self._replace(key, record)

    async def mark_uncertain(self, key: str, record: dict[str, Any]) -> None:
        await self._replace(key, record)

    async def abandon_pending(self, key: str) -> None:
        try:
            from app.core.database import async_session_factory

            async with async_session_factory() as session:
                async with session.begin():
                    await session.execute(
                        delete(EffectJournal).where(
                            EffectJournal.idempotency_key == key,
                            EffectJournal.status == "intent",
                        )
                    )
        except Exception as exc:  # noqa: BLE001
            raise EffectJournalUnavailable("副作用日志数据库不可用") from exc

    async def mark_stale_intents_uncertain(self, older_than_seconds: int) -> int:
        """Close only clearly orphaned intents during process recovery."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(1, older_than_seconds))
        try:
            from app.core.database import async_session_factory

            now = datetime.now(timezone.utc)
            async with async_session_factory() as session:
                async with session.begin():
                    result = await session.execute(
                        update(EffectJournal)
                        .where(
                            and_(
                                EffectJournal.status == "intent",
                                EffectJournal.intent_at.is_not(None),
                                EffectJournal.intent_at < cutoff,
                            )
                        )
                        .values(
                            status="uncertain",
                            reason="recovery_orphaned_intent",
                            uncertain_at=now,
                            updated_at=now,
                        )
                    )
                    return int(result.rowcount or 0)
        except Exception as exc:  # noqa: BLE001
            raise EffectJournalUnavailable("副作用日志数据库不可用") from exc


class InMemoryEffectJournalRepository:
    """Explicit test double; production code never selects it as a fallback."""

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> dict[str, Any] | None:
        async with self._lock:
            return dict(self._records[key]) if key in self._records else None

    async def record_intent(
        self, key: str, record: dict[str, Any]
    ) -> tuple[bool, dict[str, Any] | None]:
        async with self._lock:
            if key in self._records:
                return False, dict(self._records[key])
            self._records[key] = dict(record)
            return True, None

    async def confirm(self, key: str, record: dict[str, Any]) -> None:
        async with self._lock:
            if key not in self._records:
                raise EffectJournalUnavailable("副作用日志记录不存在")
            self._records[key] = dict(record)

    async def mark_uncertain(self, key: str, record: dict[str, Any]) -> None:
        await self.confirm(key, record)

    async def abandon_pending(self, key: str) -> None:
        async with self._lock:
            if self._records.get(key, {}).get("status") == "intent":
                self._records.pop(key, None)

    async def mark_stale_intents_uncertain(self, older_than_seconds: int) -> int:
        # Test double intentionally has no scheduler wall clock.
        return 0
