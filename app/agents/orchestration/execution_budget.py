"""执行预算估算。

预算是运行时保护措施，不参与意图识别、工具选择或 Skill 调度。估算依据已经
确定的执行节点，而不是用户原始文本中的关键词。
"""

from __future__ import annotations

from app.agents.orchestration.models import TaskNode


_BASE_TOKENS: dict[str, int] = {
    "direct_llm": 800,
    "atomic_step": 2_500,
    "workflow_skill": 4_000,
    "react_step": 12_000,
    "retrieval": 1_200,
    "collect_results": 1_000,
    "decision_node": 500,
}


def estimate_node_tokens(node: TaskNode) -> int:
    """Return a conservative guardrail estimate for an already-planned node."""
    params = node.params or {}
    instruction = str(
        params.get("instruction")
        or params.get("task")
        or params.get("query")
        or node.name
        or node.id
    )
    base = _BASE_TOKENS.get(str(node.agent or ""), 3_500)
    return min(20_000, base + max(1, len(instruction.strip())) // 2)
