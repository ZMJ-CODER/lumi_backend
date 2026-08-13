"""检索 agent：复用现有 RAG，检索知识库返回文档片段与引用."""

from loguru import logger

from app.agents.core.base import WorkerAgent, WorkerContext
from app.agents.orchestration.models import TaskNode
from app.agents.orchestration.progress import set_progress as _report_progress


class RetrievalAgent(WorkerAgent):
    """检索 agent：复用现成 RAG，检索知识库返回文档片段与引用."""

    name = "retrieval"
    description = "检索用户知识库，获取与问题相关的文档片段和引用"
    params_help = 'params 用 {"query": "检索词", "top_k": 5}'
    skills = ["query_knowledge"]

    async def execute(self, node: TaskNode, ctx: WorkerContext) -> dict:
        query = str(node.params.get("query") or node.params.get("request") or "").strip()
        if not query:
            return {"success": False, "error": "检索任务缺少 query 参数", "error_code": "INVALID_ARGS"}
        top_k = int(node.params.get("top_k") or 5)
        logger.debug("[Agent:retrieval] 检索 query={} top_k={}", query[:60], top_k)
        await _report_progress(ctx.job_id, node.id, "正在检索知识库…")
        result = await self.run_skill(
            "query_knowledge", {"query": query, "top_k": top_k}, ctx
        )
        if result.get("success"):
            result["step_title"] = "检索知识库"
        return result
