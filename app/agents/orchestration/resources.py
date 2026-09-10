"""Compatibility exports for the lightweight resource adapter."""

from app.agents.resource_coordination import (  # noqa: F401
    ResourceClaim,
    ResourceCoordinator,
    WriteResourceCoordinationUnavailable,
    _ACQUIRE_SCRIPT,
    _RELEASE_SCRIPT,
    _RENEW_SCRIPT,
    resource_coordinator,
)
