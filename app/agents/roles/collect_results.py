"""基于 MCP 的确定性清单汇集器工作节点封装。"""

from __future__ import annotations

from app.agents.core.base import WorkerAgent, WorkerContext
from app.agents.core.progress import set_progress


class CollectResultsAgent(WorkerAgent):
    name = "collect_results"
    description = "汇集清单原子任务结果，交给轻量模型生成最终汇报"
    params_help = '{"items":[...]}'
    skills = ["collect_results"]

    async def execute(self, node, ctx: WorkerContext) -> dict:
        items = node.params.get("items") or []
        if not isinstance(items, list):
            return {"success": False, "error": "汇集步骤缺少 items", "error_code": "INVALID_ARGS"}
        await set_progress(ctx.job_id, node.id, "正在汇集各项任务结果…")
        result = await self.run_skill("collect_results", {"items": items}, ctx)
        if result.get("success"):
            result["step_title"] = "汇集执行结果"
        return result
