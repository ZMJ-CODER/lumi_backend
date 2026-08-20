from app.agents.orchestration.models import TaskNode
from app.agents.orchestration.presentation import (
    attach_display_plan,
    attach_display_result,
    completed_text,
    intent_text,
    working_text,
)


def test_document_step_has_user_facing_plan_progress_and_result():
    node = TaskNode(
        id="n1",
        name="分析文档 sales.csv",
        agent="atomic_step",
        params={
            "preferred_tool": "office_doc_analyze",
            "inputs": {"filename": "sales.csv"},
        },
    )

    assert intent_text(node).startswith("我需要先阅读并分析文档《sales.csv》")
    assert working_text(node) == "我正在阅读并分析文档《sales.csv》。"
    assert completed_text(node, {"content": "销售额环比增长 8%"}).startswith("我已完成阅读并分析文档")

    attach_display_plan(node)
    result = attach_display_result(node, {"success": True, "content": "销售额环比增长 8%"})
    assert node.metadata["display"]["working"] == working_text(node)
    assert result["display"]["completed"].startswith("我已完成")
