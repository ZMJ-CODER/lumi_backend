"""业务任务协调器。

将提交、生命周期和控制面适配器收拢为一个稳定入口。具体服务仍保持
独立，便于渐进迁移和单元测试；上层编排器不再需要感知全部业务服务。
"""

from __future__ import annotations

from typing import Any


class JobOperationsCoordinator:
    """Facade over the business services used by ``AgentOrchestrator``."""

    def __init__(self, *, submission: Any, lifecycle: Any, control: Any) -> None:
        self.submission = submission
        self.lifecycle = lifecycle
        self.control = control

    async def submit(self, *args, **kwargs):
        return await self.submission.submit(*args, **kwargs)

    async def cancel(self, *args, **kwargs):
        return await self.control.cancel(*args, **kwargs)

    async def pause(self, *args, **kwargs):
        return await self.control.pause(*args, **kwargs)

    async def resume(self, *args, **kwargs):
        return await self.control.resume(*args, **kwargs)

    async def approve(self, *args, **kwargs):
        return await self.control.approve(*args, **kwargs)

    async def attach_progress(self, job):
        return await self.lifecycle.attach_progress(job)

    async def record_metric(self, job):
        return await self.lifecycle.record_metric(job)

    async def learn_from_finished_job(self, job):
        return await self.lifecycle.learn_from_finished_job(job)

    def discard_pending_learning(self, job_id):
        return self.lifecycle.discard_pending_learning(job_id)
