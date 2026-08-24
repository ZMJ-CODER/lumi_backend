"""Lumi infrastructure adapter for the orchestration resource kernel."""

from __future__ import annotations

from typing import Any

from lumi_orch.resources import (
    ResourceClaim,
    ResourceCoordinator as KernelResourceCoordinator,
    WriteResourceCoordinationUnavailable,  # noqa: F401 - compatibility export
    _ACQUIRE_SCRIPT,  # noqa: F401 - compatibility export
    _RELEASE_SCRIPT,  # noqa: F401 - compatibility export
    _RENEW_SCRIPT,  # noqa: F401 - compatibility export
)

from app.core.config import settings


class ResourceCoordinator(KernelResourceCoordinator):
    """Binds the generic coordinator to Lumi's Redis and settings."""

    async def _redis(self) -> Any | None:
        try:
            from app.core.redis import get_redis

            return get_redis()
        except Exception:  # noqa: BLE001
            return None

    def _requires_fail_closed(self, claim: ResourceClaim) -> bool:
        return claim.mode == "write" and bool(settings.AGENT_WRITE_RESOURCE_FAIL_CLOSED)


# Legacy process-wide entry point. Redis provides cross-process ownership.
resource_coordinator = ResourceCoordinator()
