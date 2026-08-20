"""LangGraph 节点执行图的契约测试。"""

import asyncio

from app.agents.core.base import WorkerContext
from app.agents.orchestration.langgraph_runner import LangGraphNodeRunner
from app.agents.orchestration.models import TaskNode
from app.agents.orchestration.review import NoopReviewer


def test_langgraph_retries_transient_failure_with_same_tool():
    class Worker:
        calls = 0

        async def execute(self, node, ctx):
            self.calls += 1
            if self.calls == 1:
                return {"success": False, "error": "timeout", "error_code": "TIMEOUT", "retryable": True}
            return {"success": True, "content": "ok"}

    node = TaskNode(id="n1", name="n1", agent="test", params={})
    worker = Worker()
    out = asyncio.run(
        LangGraphNodeRunner(
            worker=worker,
            node=node,
            ctx=WorkerContext(user_id="u1", job_id="j1"),
            review=NoopReviewer(),
            timeout_seconds=5,
            max_retries=1,
        ).run()
    )
    assert out.success is True
    assert out.retries == 1
    assert worker.calls == 2
    assert node.metadata.get("tool_index") is None


def test_langgraph_does_not_retry_invalid_arguments():
    class Worker:
        calls = 0

        async def execute(self, node, ctx):
            self.calls += 1
            return {"success": False, "error": "缺少 doc_id", "error_code": "INVALID_ARGS"}

    worker = Worker()
    out = asyncio.run(
        LangGraphNodeRunner(
            worker=worker,
            node=TaskNode(id="n1", name="n1", agent="test", params={}),
            ctx=WorkerContext(user_id="u1", job_id="j1"),
            review=NoopReviewer(),
            timeout_seconds=5,
            max_retries=2,
        ).run()
    )
    assert out.success is False
    assert worker.calls == 1
    assert out.recovery["replan_required"] is True


def test_langgraph_stops_immediately_when_model_balance_is_insufficient():
    class Worker:
        calls = 0

        async def execute(self, node, ctx):
            self.calls += 1
            return {
                "success": False,
                "error": "当前模型账户余额不足，办公任务已停止。",
                "error_code": "MODEL_INSUFFICIENT_BALANCE",
                "retryable": False,
            }

    worker = Worker()
    out = asyncio.run(
        LangGraphNodeRunner(
            worker=worker,
            node=TaskNode(id="n1", name="n1", agent="test", params={}),
            ctx=WorkerContext(user_id="u1", job_id="j1"),
            review=NoopReviewer(),
            timeout_seconds=5,
            max_retries=2,
        ).run()
    )
    assert out.success is False
    assert worker.calls == 1
    assert out.recovery["user_action_required"] is True
