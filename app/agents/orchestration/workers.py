"""执行层 WorkerAgent —— 领取 DAG 节点任务，调用技能执行并返回结构化结果.

一个 WorkerAgent = 一个专业角色 + 一组可调用的技能（复用技能插件体系）。
新增执行 agent 时继承 WorkerAgent 并注册到 WORKERS。
"""

import re
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
    # BYOK：agent 任务提交时临时携带的 API key（内存持有，任务结束即释放，不落库）
    llm_api_key: str | None = None


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


class CodeAgent(WorkerAgent):
    """代码 agent：方案 A —— 在用户本地项目里定位 → 读取 → LLM 生成 → 写回.

    文件读写走 client 技能（Electron 本地执行，路径 jail 到项目根）；
    定位用服务器端结构索引（不读代码正文）。
    """

    name = "code"
    description = "根据指令读写本地代码项目，生成/修改代码并运行测试"
    skills = ["list_project", "read_project_file", "write_project_file", "run_project_command"]

    _SYSTEM_PROMPT = (
        "你是一名资深软件工程师，正在用户的本地代码项目里工作。"
        "根据用户指令和提供的文件内容，生成修改后的完整文件内容。"
        "只输出文件内容本身，不要解释、不要 Markdown 代码块围栏。"
    )

    async def execute(self, node: TaskNode, ctx: WorkerContext) -> dict:
        project_id = str(node.params.get("project_id") or "")
        instruction = str(node.params.get("instruction") or "").strip()
        if not project_id or not instruction:
            return {
                "success": False,
                "error": "缺少 project_id 或 instruction",
                "error_code": "INVALID_ARGS",
            }

        # 1. 定位相关文件（显式指定 或 检索结构索引）
        target = str(node.params.get("target_file") or "")
        if not target:
            target = await self._locate(project_id, instruction, ctx) or ""
        if not target:
            return {
                "success": False,
                "error": "未能在项目索引中定位相关文件，请明确要修改的文件",
                "error_code": "EXEC_ERROR",
            }

        # 2. 读取（client 技能，本地执行）
        read = await self.run_skill(
            "read_project_file", {"project_id": project_id, "path": target}, ctx
        )
        if not read.get("success"):
            return read
        original = str(read.get("content") or "")

        # 3. LLM 生成修改后的完整内容
        new_content = await self._generate(ctx, instruction, target, original)
        if not new_content:
            return {
                "success": False,
                "error": "模型未能生成修改内容",
                "error_code": "EXEC_ERROR",
            }

        # 4. 写入（client 技能，确认弹窗）
        return await self.run_skill(
            "write_project_file",
            {"project_id": project_id, "path": target, "content": new_content},
            ctx,
        )

    async def _locate(self, project_id: str, instruction: str, ctx: WorkerContext) -> str | None:
        """用结构索引检索相关文件（服务器只存路径/符号/摘要）."""
        from app.core.database import async_session_factory
        from app.services import project_index

        query = re.sub(
            r"[帮我在项目里请把请帮我实现修改创建添加删除优化重构修复检查看看读取一下写一个设计新增]",
            "",
            instruction,
        )
        query = " ".join(query.split())[:100] or instruction[:100]
        try:
            async with async_session_factory() as session:
                hits = await project_index.search_project(
                    session, ctx.user_id, project_id, query, limit=5
                )
            if hits:
                return hits[0]["file_path"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Agent:code] 定位文件失败: {}", exc)
        return None

    async def _generate(
        self, ctx: WorkerContext, instruction: str, path: str, original: str
    ) -> str:
        try:
            from app.core.llm import LLMClient
            from app.services.usage import CATEGORY_CODE

            llm = LLMClient()
            content = original[-60000:] if len(original) > 60000 else original
            reply = await llm.chat(
                [
                    {"role": "system", "content": self._SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"用户指令：{instruction}\n\n"
                            f"文件路径：{path}\n\n当前文件内容：\n{content}"
                        ),
                    },
                ],
                max_tokens=8000,
                usage_user_id=ctx.user_id,
                usage_category=CATEGORY_CODE,
                api_key=ctx.llm_api_key,
            )
            reply = (reply or "").strip()
            if reply.startswith("```"):
                reply = re.sub(r"^```\w*\n?", "", reply)
                reply = re.sub(r"\n?```$", "", reply)
            return reply
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Agent:code] LLM 生成失败: {}", exc)
            return ""


# 执行层 agent 注册表（按 name 路由）
WORKERS: dict[str, WorkerAgent] = {
    "retrieval": RetrievalAgent(),
    "code": CodeAgent(),
}


def get_worker(name: str) -> WorkerAgent | None:
    return WORKERS.get(name)


def list_workers() -> list[WorkerAgent]:
    return list(WORKERS.values())
