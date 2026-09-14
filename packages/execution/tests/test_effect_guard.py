from __future__ import annotations

import pytest

from lumi_execution.effects import EffectGuard


class Journal:
    def __init__(self) -> None:
        self.records: dict[str, dict] = {}

    async def reserve(self, key, intent=None):
        if key in self.records:
            return False, self.records[key]
        self.records[key] = {"status": "pending", "intent": intent or {}}
        return True, None

    async def confirm(self, key, result=None):
        self.records[key] = {"status": "confirmed", "result": result}

    async def mark_uncertain(self, key, reason):
        self.records[key] = {"status": "uncertain", "reason": reason}

    async def abandon_pending(self, key):
        self.records.pop(key, None)


@pytest.mark.asyncio
async def test_effect_guard_supports_uncertain_and_pending_cleanup_transitions():
    journal = Journal()
    guard = EffectGuard(journal)

    assert await guard.reserve("k", {"node": "n"}) is None
    await guard.mark_uncertain("k", "worker_restart")
    with pytest.raises(RuntimeError, match="effect intent exists"):
        await guard.reserve("k")

    await guard.abandon_pending("k")
    assert await guard.reserve("k") is None
    await guard.confirm("k", {"ok": True})
    existing = await guard.reserve("k")
    assert existing == {"status": "confirmed", "result": {"ok": True}}
