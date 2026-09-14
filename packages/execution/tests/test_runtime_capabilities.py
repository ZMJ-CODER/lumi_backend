import asyncio

from lumi_execution import NodeExecutionMetrics, ResourceDispatcher, RetryBudget


def test_retry_budget_is_bounded():
    budget = RetryBudget(max_retries=2, budget_seconds=3)
    assert budget.delay_for(0) == 1
    assert budget.can_retry(0, next_delay=1)
    assert not budget.can_retry(0, next_delay=4)


def test_resource_dispatcher_separates_classes():
    async def run():
        dispatcher = ResourceDispatcher({"cpu_bound": 1, "io_bound": 2})
        async with dispatcher.claim("cpu_bound"):
            async with dispatcher.claim("io_bound"):
                return True

    assert asyncio.run(run()) is True


def test_metrics_contract_is_serializable():
    metrics = NodeExecutionMetrics(node_id="n1", retries=1)
    assert metrics.node_id == "n1"

