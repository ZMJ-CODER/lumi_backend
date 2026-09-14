"""Desktop MCP endpoint resolution.

The execution layer depends on this small registry rather than on a hardcoded
localhost URL.  Today it resolves configured loopback Streamable-HTTP servers;
a future desktop WebSocket relay only needs to provide another resolver and
does not change Tool/Skill/Orchestrator contracts.

Device routing contract
-----------------------
Each workspace registration records the desktop that hosts it
(``device_id`` / ``device_server``, see ``app.workspace.service``).  A task
started from any device is routed back to that desktop by resolving the MCP
server row that belongs to the workspace's registered device:

    conversation_id -> workspace_id -> device_id -> DesktopConnectionRegistry
        -> Electron MCP connection

Configured rows (``settings.MCP_SERVERS``) may carry ``user_id``/``device_id``
filters.  Resolution prefers the most specific row that does not exclude the
caller: device+user match > user match > shared row (no filter).  Row ``name``
must stay unique per device so cache/breaker/session keys never collide.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.config import settings


@dataclass(frozen=True, slots=True)
class DesktopEndpoint:
    name: str
    transport: str
    url: str
    device_id: str = ""
    user_id: str = ""


def _row_matches_user(raw: dict, user_id: str) -> bool:
    bound = str(raw.get("user_id") or "").strip()
    return not bound or not user_id or bound == user_id


def _row_matches_device(raw: dict, device_id: str) -> bool:
    bound = str(raw.get("device_id") or "").strip()
    return not bound or not device_id or bound == device_id


def _is_desktop_row(raw: dict) -> bool:
    name = str(raw.get("name") or "")
    return bool(name) and (
        str(raw.get("provider_type") or "") == "desktop_mcp" or name == "lumi_client"
    )


def _to_endpoint(raw: dict, *, user_id: str = "", device_id: str = "") -> DesktopEndpoint | None:
    url = str(raw.get("url") or "").strip()
    name = str(raw.get("name") or "").strip()
    if not url or not name:
        return None
    return DesktopEndpoint(
        name=name,
        transport=str(raw.get("transport") or "streamable-http"),
        url=url,
        user_id=str(user_id or raw.get("user_id") or ""),
        device_id=str(device_id or raw.get("device_id") or ""),
    )


class DesktopConnectionRegistry:
    """Resolve a permitted desktop endpoint without exposing local paths."""

    def resolve(self, name: str, *, user_id: str = "", device_id: str = "") -> DesktopEndpoint | None:
        """Resolve one configured row by its unique server ``name``."""
        for raw in settings.MCP_SERVERS or []:
            if str(raw.get("name") or "") != str(name):
                continue
            url = str(raw.get("url") or "").strip()
            if not url:
                return None
            # Deployment safety: when a row pins user/device, an explicit
            # caller identity must agree with it before the row is used.
            if not _row_matches_user(raw, user_id) or not _row_matches_device(raw, device_id):
                continue
            return _to_endpoint(raw, user_id=user_id, device_id=device_id)
        return None

    def resolve_desktop(
        self,
        *,
        user_id: str = "",
        device_id: str = "",
        preferred_name: str = "",
    ) -> DesktopEndpoint | None:
        """Pick the desktop Electron row for ``(user_id, device_id)``.

        All rows that do not *exclude* the caller are candidates; the most
        specific row wins (device+user bound > device/user bound > shared
        row), with ties broken by deployment order.  ``preferred_name`` lets
        a workspace pin its registered server row first.
        """
        rows = [raw for raw in (settings.MCP_SERVERS or []) if _is_desktop_row(raw)]
        if preferred_name:
            for raw in rows:
                if str(raw.get("name") or "") == preferred_name and _row_matches_user(raw, user_id):
                    endpoint = _to_endpoint(raw, user_id=user_id, device_id=device_id)
                    if endpoint is not None:
                        return endpoint
        best: DesktopEndpoint | None = None
        best_score = -1
        for raw in rows:
            if not _row_matches_user(raw, user_id) or not _row_matches_device(raw, device_id):
                continue
            score = (1 if str(raw.get("user_id") or "").strip() else 0) + (
                1 if str(raw.get("device_id") or "").strip() else 0
            )
            endpoint = _to_endpoint(raw, user_id=user_id, device_id=device_id)
            if endpoint is None:
                continue
            if score > best_score:
                best, best_score = endpoint, score
        return best

    def config_for(self, name: str, *, user_id: str = "", device_id: str = "") -> dict[str, Any] | None:
        endpoint = self.resolve(name, user_id=user_id, device_id=device_id)
        if endpoint is None:
            return None
        return {
            "name": endpoint.name,
            "transport": endpoint.transport,
            "url": endpoint.url,
            "device_id": endpoint.device_id,
            "user_id": endpoint.user_id,
        }

    def desktop_server_names(self, *, user_id: str = "", device_id: str = "") -> list[str]:
        """Return deployment-approved Electron Tool providers.

        Pass ``user_id``/``device_id`` to limit the set to rows that may
        belong to the caller (rows pinned to another user/device are
        excluded).
        """
        names: list[str] = []
        for raw in settings.MCP_SERVERS or []:
            if not _is_desktop_row(raw):
                continue
            if not _row_matches_user(raw, user_id) or not _row_matches_device(raw, device_id):
                continue
            name = str(raw.get("name") or "")
            if name and name not in names:
                names.append(name)
        return names


desktop_connections = DesktopConnectionRegistry()
