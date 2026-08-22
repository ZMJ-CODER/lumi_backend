import asyncio

from app.agents.orchestration.plan_context import PlanRequestContext
from app.agents.orchestration.planner import Planner, TaskTree


def test_plan_request_context_normalizes_legacy_collections():
    context = PlanRequestContext.from_legacy_args(
        "u1",
        "处理文件",
        project_ids=["p1", 2, ""],
        office_docs=[{"doc_id": "d1", "filename": "a.txt"}],
    )

    assert context.project_ids == ("p1", "2")
    assert context.office_docs == ({"doc_id": "d1", "filename": "a.txt"},)
    assert context.as_legacy_args()[4] == ["p1", "2"]
    assert context.as_legacy_args()[7] == [{"doc_id": "d1", "filename": "a.txt"}]


def test_planner_context_adapter_keeps_custom_plan_signature():
    class CustomPlanner(Planner):
        async def plan(self, *args, **kwargs):
            assert args[:2] == ("u1", "查询")
            assert len(args) == 9
            assert kwargs == {}
            return TaskTree(nodes=[])

    context = PlanRequestContext(user_id="u1", request="查询")
    tree = asyncio.run(CustomPlanner().plan_context(context))
    assert tree.nodes == []
