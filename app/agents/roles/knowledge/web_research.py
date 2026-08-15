"""联网研究 agent：搜索互联网获取最新信息并返回带来源的结果."""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from app.agents.core.base import WorkerAgent, WorkerContext
from app.agents.core.progress import set_progress as _report_progress

if TYPE_CHECKING:
    from app.agents.orchestration.models import TaskNode


class WebResearchAgent(WorkerAgent):
    """联网研究 agent：调用 web_search 技能获取实时信息，返回带来源的结果."""

    name = "web_research"
    description = "联网搜索互联网获取最新/实时信息（新闻、数据、当前事件等），返回带来源的结果"
    params_help = 'params 用 {"query": "搜索问题"}'
    skills = ["web_search"]

    async def execute(self, node: TaskNode, ctx: WorkerContext) -> dict:
        query = str(node.params.get("query") or node.params.get("request") or "").strip()
        if not query:
            return {
                "success": False,
                "error": "联网研究任务缺少 query 参数",
                "error_code": "INVALID_ARGS",
            }
        top_k = int(node.params.get("top_k") or 5)
        logger.debug("[Agent:web_research] 联网搜索 query={} top_k={}", query[:60], top_k)
        await _report_progress(ctx.job_id, node.id, "正在联网搜索…")
        result = await self.run_skill(
            "web_search", {"query": query, "max_results": top_k}, ctx
        )
        if result.get("success"):
            result["step_title"] = "联网搜索"
        return result
