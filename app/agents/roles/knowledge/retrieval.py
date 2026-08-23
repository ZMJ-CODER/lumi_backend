"""检索 agent：复用现有 RAG，检索知识库返回文档片段与引用."""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from app.agents.core.base import WorkerAgent, WorkerContext
from app.agents.core.progress import set_progress as _report_progress

if TYPE_CHECKING:
    from app.agents.orchestration.models import TaskNode


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
        # Safety net: even if an upstream phrase classifier missed a
        # multi-document fact request, never query the unrestricted knowledge
        # index before narrowing the server-authorized attachment scope.
        if len(ctx.office_doc_ids) >= 2:
            from app.agents.roles.knowledge.document_targeting import DocumentTargetingAgent

            targeted = await DocumentTargetingAgent().execute(node, ctx)
            if targeted.get("success"):
                targeted["step_title"] = "定位并检索已授权文档"
            return targeted
        logger.debug("[Agent:retrieval] 检索 query={} top_k={}", query[:60], top_k)
        await _report_progress(ctx.job_id, node.id, "正在检索知识库…")
        result = await self.run_skill(
            "query_knowledge", {"query": query, "top_k": top_k}, ctx
        )
        if not result.get("success"):
            # A search miss is not automatically an Agent problem: Agent must
            # not invent private/system access.  Only a capability signal from
            # the narrow retrieval path asks the scheduler to broaden the
            # read-only atom, and it remains bounded by the manifest upgrade
            # policy.
            code = str(result.get("error_code") or "").upper()
            if code in {"CAPABILITY_UNAVAILABLE", "MCP_UNAVAILABLE", "SKILL_NOT_FOUND"}:
                return {
                    **result,
                    "error_code": "ROUTE_UPGRADE_AGENT",
                    "error": "检索通道不可用，正在改由动态执行通道处理",
                    "retryable": False,
                }
            return result
        if result.get("success"):
            result["step_title"] = "检索知识库"
        return result
