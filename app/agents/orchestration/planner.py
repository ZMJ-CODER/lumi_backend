"""指挥层 Planner —— 用户意图 → 任务树（DAG）.

框架版：RulePlanner 把请求直接映射为单个检索节点，跑通链路。
后续接入 LLM 意图拆解：function calling 输出结构化任务树 JSON，
支持"意图不明确时向用户反问"（最多 2~3 轮澄清）。
"""

import time
import uuid
from abc import ABC, abstractmethod

from app.agents.orchestration.models import TaskNode


class TaskTree:
    """规划结果：一组带依赖的任务节点."""

    def __init__(self, nodes: list[TaskNode]):
        self.nodes = nodes


class Planner(ABC):
    """指挥层基类."""

    @abstractmethod
    async def plan(self, user_id: str, request: str, scene: str = "office") -> TaskTree:
        ...


class RulePlanner(Planner):
    """框架版规划器：检索类请求 → 单个 retrieval 节点.

    后续：
      - LLM 意图拆解（function calling → 任务树 JSON）
      - 多节点 DAG（检索 → 分析 → 文档产出）
      - 意图不明确时反问用户（限 2~3 轮）
    """

    async def plan(self, user_id: str, request: str, scene: str = "office") -> TaskTree:
        node = TaskNode(
            id=f"t{int(time.time())}-{uuid.uuid4().hex[:6]}",
            name="知识库检索",
            agent="retrieval",
            params={"query": request, "top_k": 5},
            depends_on=[],
        )
        return TaskTree(nodes=[node])
