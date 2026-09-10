"""Lightweight application adapter for the orchestration resource kernel.

Atomic tool execution needs resource leases, but importing the orchestration
package also initializes Planner/LangGraph.  Keeping this adapter outside that
package prevents the first tool call from paying the whole planner import cost.
"""

from __future__ import annotations

from typing import Any

from lumi_orch.resources import (
    ResourceClaim,
    ResourceCoordinator as KernelResourceCoordinator,
    WriteResourceCoordinationUnavailable,
    _ACQUIRE_SCRIPT,
    _RELEASE_SCRIPT,
    _RENEW_SCRIPT,
)

from app.core.config import settings


class ResourceCoordinator(KernelResourceCoordinator):
    """Bind the generic coordinator to Lumi Redis and fail-closed settings."""

    async def _redis(self) -> Any | None:
        try:
            from app.core.redis import get_redis

            return get_redis()
        except Exception:  # noqa: BLE001
            return None

    def _requires_fail_closed(self, claim: ResourceClaim) -> bool:
        return claim.mode == "write" and bool(settings.AGENT_WRITE_RESOURCE_FAIL_CLOSED)


resource_coordinator = ResourceCoordinator()


__all__ = [
    "ResourceClaim",
    "ResourceCoordinator",
    "WriteResourceCoordinationUnavailable",
    "resource_coordinator",
    "_ACQUIRE_SCRIPT",
    "_RELEASE_SCRIPT",
    "_RENEW_SCRIPT",
]
