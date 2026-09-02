"""Regression tests for the durable two-phase external-effect journal."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from app.agents.orchestration.effects import (
    EffectJournalUnavailable,
    confirm_effect,
    get_effect,
    record_effect_intent,
    set_effect_journal_repository_for_tests,
)
from app.agents.orchestration.models import Job, JobStatus, ResourceClaim, TaskNode, TaskStatus
from app.agents.orchestration.review import NoopReviewer
from app.agents.orchestration.state import InMemoryStateStore
from app.repositories.effect_journal_repository import InMemoryEffectJournalRepository


def setup_function() -> None:
    set_effect_journal_repository_for_tests(InMemoryEffectJournalRepository())


def teardown_function() -> None:
    set_effect_journal_repository_for_tests(None)


def test_effect_intent_is_idempotent_and_confirmation_is_durable_contract():
    async def scenario() -> None:
        created, existing = await record_effect_intent("effect-1", {"job_id": "j1", "node_id": "n1"})
        assert created is True
        assert existing is None

        created, existing = await record_effect_intent("effect-1", {"job_id": "j1", "node_id": "n1"})
        assert created is False
        assert existing and existing["status"] == "intent"

        await confirm_effect("effect-1", {"content": "sent"})
        record = await get_effect("effect-1")
        assert record and record["status"] == "confirmed"
        assert record["result"] == {"content": "sent"}

    asyncio.run(scenario())


def test_effectful_node_fails_closed_without_executing_worker_when_journal_is_unavailable(monkeypatch):
    from app.agents.orchestration.execution.validation import execute_dag
    from app.agents.orchestration import resources

    class UnavailableJournal:
        async def get(self, _key):
            raise EffectJournalUnavailable("down")

        async def record_intent(self, _key, _record):
            raise EffectJournalUnavailable("down")

        async def confirm(self, _key, _record):
            raise EffectJournalUnavailable("down")

        async def mark_uncertain(self, _key, _record):
            raise EffectJournalUnavailable("down")

        async def abandon_pending(self, _key):
            raise EffectJournalUnavailable("down")

        async def mark_stale_intents_uncertain(self, _seconds):
            raise EffectJournalUnavailable("down")

    class WriteWorker:
        def __init__(self):
            self.calls = 0

        async def execute(self, node, ctx):
            self.calls += 1
            return {"success": True, "content": "must not happen"}

    class AvailableCoordinator:
        async def write_coordination_available(self, _claims):
            return True

        @asynccontextmanager
        async def claim(self, _claims, ttl=60):
            yield

    set_effect_journal_repository_for_tests(UnavailableJournal())
    monkeypatch.setattr(resources, "resource_coordinator", AvailableCoordinator())
    worker = WriteWorker()
    node = TaskNode(
        id="write",
        name="write",
        agent="writer",
        resource_claims=[ResourceClaim(key="document:1", mode="write")],
        idempotency_key="effect-unavailable",
    )
    store = InMemoryStateStore()
    job = Job(job_id="effect-journal-job", user_id="u1", request="write", nodes=[node], status=JobStatus.RUNNING)
    asyncio.run(execute_dag(job, {"writer": worker}, NoopReviewer(), store))
    final = asyncio.run(store.get_job(job.job_id))
    assert worker.calls == 0
    assert final and final.nodes[0].status == TaskStatus.FAILED
    assert final.nodes[0].error_code == "EFFECT_JOURNAL_UNAVAILABLE"
