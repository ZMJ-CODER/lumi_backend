"""质检钩子 —— 工具执行结果的质量校验.

框架版：NoopReviewer 一律放行；
后续接入 LLM 质检（轻量模型 + 分级检查：轻量必做 / 深度按任务类型）。
"""

from abc import ABC, abstractmethod

from app.agents.orchestration.models import ReviewVerdict, TaskNode
from app.agents.orchestration.workers import WorkerContext


class ReviewHook(ABC):
    """质检接口."""

    @abstractmethod
    async def review(self, node: TaskNode, result: dict, ctx: WorkerContext) -> ReviewVerdict:
        ...


class NoopReviewer(ReviewHook):
    """框架占位：不校验，一律通过."""

    async def review(self, node: TaskNode, result: dict, ctx: WorkerContext) -> ReviewVerdict:
        return ReviewVerdict(approved=True)


REVIEWER: ReviewHook = NoopReviewer()


def get_reviewer() -> ReviewHook:
    return REVIEWER
