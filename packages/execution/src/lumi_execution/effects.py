"""与运行时后端无关的两阶段副作用保护器。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class EffectJournalPort(Protocol):
    async def reserve(self, key: str, intent: Mapping[str, Any] | None = None) -> tuple[bool, Mapping[str, Any] | None]: ...
    async def confirm(self, key: str, result: Mapping[str, Any] | None = None) -> None: ...

    async def mark_uncertain(self, key: str, reason: str) -> None: ...

    async def abandon_pending(self, key: str) -> None: ...


class EffectGuard:
    """Reserve before a side effect and make confirmed retries read-only."""

    def __init__(self, journal: EffectJournalPort) -> None:
        self._journal = journal

    async def reserve(self, key: str, intent: Mapping[str, Any] | None = None) -> Mapping[str, Any] | None:
        created, previous = await self._journal.reserve(key, intent)
        if created:
            return None
        if str((previous or {}).get("status")) in {"confirmed", "committed"}:
            return previous
        raise RuntimeError("effect intent exists without confirmation")

    async def confirm(self, key: str, result: Mapping[str, Any] | None = None) -> None:
        await self._journal.confirm(key, result)

    async def mark_uncertain(self, key: str, reason: str = "execution_interrupted") -> None:
        method = getattr(self._journal, "mark_uncertain", None)
        if method is None:
            raise RuntimeError("effect journal does not support uncertain transition")
        await method(key, reason)

    async def abandon_pending(self, key: str) -> None:
        method = getattr(self._journal, "abandon_pending", None)
        if method is None:
            raise RuntimeError("effect journal does not support pending cleanup")
        await method(key)
