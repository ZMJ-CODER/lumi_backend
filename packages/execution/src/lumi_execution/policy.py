"""进程内与 Temporal 运行时共用的执行策略原语。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lumi_orch.job_spec import NodeExecutionSpec


@dataclass(frozen=True, slots=True)
class ResolvedExecutionPolicy:
    """A frozen policy snapshot consumed by the executor."""

    spec: NodeExecutionSpec
    version: int = 1
    sha256: str = ""

    @property
    def max_retries(self) -> int:
        return max(0, int(self.spec.retry.max_attempts or 1) - 1)

    @property
    def timeout_seconds(self) -> int | None:
        return self.spec.timeout_seconds

    @property
    def metadata(self) -> dict[str, Any]:
        return {"version": self.version, "sha256": self.sha256, "resolved": self.spec.model_dump(mode="json")}
