"""执行层 WorkerAgent —— 领取 DAG 节点任务，调用技能执行并返回结构化结果.

一个 WorkerAgent = 一个专业角色 + 一组可调用的技能（复用技能插件体系）。
新增执行 agent 时继承 WorkerAgent 并注册到 WORKERS。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from loguru import logger

from app.agents.orchestration.models import TaskNode
from app.agents.skills.base import SkillContext
from app.agents.skills.registry import SkillRegistry


@dataclass
class WorkerContext:
    """Worker 执行上下文（编排器注入）."""

    user_id: str
    job_id: str
    scene: str = "office"


class WorkerAgent(ABC):
    """执行层 agent 基类."""

    name: str = ""
    description: str = ""
    skills: list[str] = []  # 该 agent 可调用的技能名（白名单）

    @abstractmethod
    async def execute(self, node: TaskNode, ctx: WorkerContext) -> dict:
        """执行一个任务节点，返回结构化结果."""
        ...

    async def run_skill(self, skill_name: str, params: dict, ctx: WorkerContext) -> dict:
        """调用技能并统一包装结果（带审计，复用技能体系）."""
        skill = SkillRegistry.get(skill_name)
        if skill is None:
            return {"success": False, "error": f"技能不存在: {skill_name}", "error_code": "SKILL_NOT_FOUND"}
        if self.skills and skill_name not in self.skills:
            return {"success": False, "error": f"agent '{self.name}' 无权调用技能 {skill_name}", "error_code": "FORBIDDEN"}
        result = await skill.execute(
            params,
            SkillContext(user_id=ctx.user_id, scene=ctx.scene, conversation_id=ctx.job_id),
        )
        if not result.success:
            return {"success": False, "error": result.error, "error_code": result.error_code}
        return {"success": True, "content": result.output, **result.metadata}

    def __repr__(self) -> str:
        return f"<WorkerAgent: {self.name} skills={self.skills}>"


class RetrievalAgent(WorkerAgent):
    """检索 agent：复用现成 RAG，检索知识库返回文档片段与引用."""

    name = "retrieval"
    description = "检索用户知识库，获取与问题相关的文档片段和引用"
    skills = ["query_knowledge"]

    async def execute(self, node: TaskNode, ctx: WorkerContext) -> dict:
        query = str(node.params.get("query") or node.params.get("request") or "").strip()
        if not query:
            return {"success": False, "error": "检索任务缺少 query 参数", "error_code": "INVALID_ARGS"}
        top_k = int(node.params.get("top_k") or 5)
        logger.debug("[Agent:retrieval] 检索 query={} top_k={}", query[:60], top_k)
        return await self.run_skill(
            "query_knowledge", {"query": query, "top_k": top_k}, ctx
        )


# 执行层 agent 注册表（按 name 路由）
WORKERS: dict[str, WorkerAgent] = {
    "retrieval": RetrievalAgent(),
}


def get_worker(name: str) -> WorkerAgent | None:
    return WORKERS.get(name)


def list_workers() -> list[WorkerAgent]:
    return list(WORKERS.values())
