import asyncio

import pytest

from lumi_orch.resources import (
    ResourceClaim,
    ResourceCoordinator,
    WriteResourceCoordinationUnavailable,
)


def test_local_claims_are_reader_shared_and_writer_exclusive():
    async def acquire_writer(coordinator, entered_writer):
        async with coordinator.claim([ResourceClaim(key="doc:1", mode="write")]):
            entered_writer.set()

    async def scenario():
        coordinator = ResourceCoordinator()
        entered_writer = asyncio.Event()
        async with coordinator.claim([ResourceClaim(key="doc:1", mode="read")]):
            async with coordinator.claim([ResourceClaim(key="doc:1", mode="read")]):
                writer = asyncio.create_task(acquire_writer(coordinator, entered_writer))
                await asyncio.sleep(0)
                assert not entered_writer.is_set()
            assert not entered_writer.is_set()
        await writer
        assert entered_writer.is_set()

    asyncio.run(scenario())


def test_write_claim_fails_closed_without_healthy_distributed_backend():
    coordinator = ResourceCoordinator(fail_closed=lambda claim: claim.mode == "write")

    async def scenario():
        assert await coordinator.write_coordination_available([ResourceClaim(key="doc:1", mode="write")]) is False
        with pytest.raises(WriteResourceCoordinationUnavailable):
            async with coordinator.claim([ResourceClaim(key="doc:1", mode="write")]):
                pass

    asyncio.run(scenario())
