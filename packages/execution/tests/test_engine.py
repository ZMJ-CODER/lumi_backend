import asyncio
from dataclasses import dataclass

from lumi_execution import ExecutionEngine


@dataclass
class Verdict:
    approved: bool = True
    feedback: str = ""


class Review:
    async def review(self, _node, _result, _context):
        return Verdict()


class Policy:
    def decide(self, _code, _error, *, retryable, **_kwargs):
        from lumi_execution import FailureDecision

        return FailureDecision(retry_same=retryable, category="transient")


class Worker:
    def __init__(self):
        self.calls = 0

    async def execute(self, _node, _context):
        self.calls += 1
        if self.calls == 1:
            return {"success": False, "error": "temporary", "error_code": "TIMEOUT", "retryable": True}
        return {"content": "ok"}


def test_engine_retries_without_runtime_dependencies():
    worker = Worker()
    outcome = asyncio.run(
        ExecutionEngine(
            executor=worker,
            node=object(),
            context=object(),
            review=Review(),
            failure_policy=Policy(),
            timeout_seconds=2,
            max_retries=1,
        ).run()
    )
    assert outcome.success is True
    assert outcome.retries == 1
    assert worker.calls == 2
