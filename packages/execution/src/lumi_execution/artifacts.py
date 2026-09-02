"""结果大小边界：大载荷以引用形式表示。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    name: str
    uri: str
    content_type: str | None = None
    size_bytes: int | None = None


class ArtifactStore(Protocol):
    async def put(self, name: str, value: Any) -> ArtifactRef: ...
