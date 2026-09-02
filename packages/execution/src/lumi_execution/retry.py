"""确定性的重试预算与退避时间计算。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class RetryBudget:
    max_retries: int
    budget_seconds: float | None = None
    backoff: str = "exponential_jitter"
    elapsed_seconds: float = 0.0

    def can_retry(self, retry_count: int, *, next_delay: float = 0.0) -> bool:
        if retry_count >= max(0, self.max_retries):
            return False
        return self.budget_seconds is None or self.elapsed_seconds + next_delay <= self.budget_seconds

    def delay_for(self, retry_count: int) -> float:
        """Return a bounded deterministic delay; wall-clock sleeping is adapter-owned."""
        if self.backoff == "fixed":
            return 1.0
        return float(min(30, 2 ** max(0, retry_count)))

    def consume(self, seconds: float) -> None:
        self.elapsed_seconds += max(0.0, float(seconds))
