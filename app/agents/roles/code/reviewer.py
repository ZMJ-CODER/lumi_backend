"""代码审查 agent：审查已有代码或改动，输出结构化问题清单（供质检/人工参考）."""

from app.agents.core.base import WorkerAgent, WorkerContext
from app.agents.core.tools import locate_project_file, review_code_content
from app.agents.orchestration.models import TaskNode


class CodeReviewerAgent(WorkerAgent):
    """代码审查 agent：审查已有代码或改动，输出结构化问题清单（供质检/人工参考）."""

    name = "code_reviewer"
    description = "审查本地项目代码或改动，输出是否通过、问题清单与反馈"
    params_help = (
        'params 用 {"project_id": "项目ID", "instruction": "审查要求", "target_file": "可选文件路径"}'
    )
    skills = ["read_project_file"]

    async def execute(self, node: TaskNode, ctx: WorkerContext) -> dict:
        project_id = str(node.params.get("project_id") or "")
        instruction = str(node.params.get("instruction") or "").strip()
        if not project_id:
            return {
                "success": False,
                "error": "缺少 project_id",
                "error_code": "INVALID_ARGS",
            }

        content = node.params.get("content")
        path_label = str(
            node.params.get("target_file")
            or node.params.get("file_key")
            or node.params.get("path")
            or ""
        )
        if content is None:
            located = await locate_project_file(
                ctx.user_id,
                project_id,
                instruction,
                node.params.get("target_file"),
                node.params.get("file_key"),
            )
            if not located or not (located.get("path") or located.get("file_key")):
                return {
                    "success": False,
                    "error": "未能在项目索引中定位相关文件",
                    "error_code": "EXEC_ERROR",
                }
            path_label = str(located.get("path") or located.get("file_key"))
            read_params = {"project_id": project_id}
            if located.get("file_key"):
                read_params["file_key"] = located["file_key"]
            else:
                read_params["path"] = located["path"]
            read = await self.run_skill("read_project_file", read_params, ctx)
            if not read.get("success"):
                return read
            content = read.get("content") or ""

        review = await review_code_content(ctx, instruction, path_label, content or "")
        return {"success": True, "path": path_label, **review}
