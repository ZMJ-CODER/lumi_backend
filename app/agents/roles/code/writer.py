"""编码 agent：基于指令与代码上下文生成/修改本地文件并写回项目."""

from app.agents.core.base import WorkerAgent, WorkerContext
from app.agents.core.tools import (
    CODE_SYSTEM_PROMPT,
    _read_with_retry,
    _step_title,
    generate_code_content,
    list_project_files,
    locate_project_file,
)
from app.agents.orchestration.models import TaskNode
from app.agents.orchestration.progress import set_progress as _report_progress


class CodeWriterAgent(WorkerAgent):
    """编码 agent：基于指令与代码上下文生成/修改本地文件并写回项目."""

    name = "code_writer"
    description = "根据指令生成或修改本地代码文件内容并写回项目"
    params_help = (
        'params 用 {"project_id": "项目ID", "instruction": "编码指令", '
        '"target_file": "可选文件路径", "original_content": "可选，来自 reader"}'
    )
    skills = ["read_project_file", "write_project_file"]

    _SYSTEM_PROMPT = CODE_SYSTEM_PROMPT

    async def execute(self, node: TaskNode, ctx: WorkerContext) -> dict:
        project_id = str(node.params.get("project_id") or "")
        instruction = str(node.params.get("instruction") or "").strip()
        if not project_id or not instruction:
            return {
                "success": False,
                "error": "缺少 project_id 或 instruction",
                "error_code": "INVALID_ARGS",
            }

        target_path = node.params.get("target_file")
        target_key = node.params.get("file_key")
        original = node.params.get("original_content")
        path_label = str(target_path or target_key or "")

        # 上游（如 code_reader）未传内容时，自行定位并读取
        if original is None:
            located = await locate_project_file(
                ctx.user_id, project_id, instruction, target_path, target_key
            )
            if not located or not (located.get("path") or located.get("file_key")):
                return {
                    "success": False,
                    "error": "未能在项目索引中定位相关文件，请明确要修改的文件",
                    "error_code": "EXEC_ERROR",
                }
            target_path = located.get("path")
            target_key = located.get("file_key")
            path_label = str(target_path or target_key)
            read, located = await _read_with_retry(
                self, ctx, project_id, instruction, target_path, target_key
            )
            if not read.get("success"):
                return read
            if located:
                target_path = located.get("path") or target_path
                target_key = located.get("file_key") or target_key
                path_label = str(target_path or target_key)
            original = read.get("content") or ""

        await _report_progress(ctx.job_id, node.id, f"正在生成 {path_label} 代码…")
        new_content = await generate_code_content(
            ctx,
            instruction,
            path_label,
            original or "",
            self._SYSTEM_PROMPT,
            project_files=await list_project_files(ctx.user_id, project_id),
        )
        if not new_content:
            return {
                "success": False,
                "error": "模型未能生成修改内容",
                "error_code": "EXEC_ERROR",
            }

        write_params = {"project_id": project_id}
        if target_path:
            write_params["path"] = target_path
        else:
            write_params["file_key"] = target_key
        await _report_progress(ctx.job_id, node.id, f"正在写入 {path_label}…")
        write_result = await self.run_skill(
            "write_project_file", {**write_params, "content": new_content}, ctx
        )
        if write_result.get("success"):
            write_result["new_content"] = new_content
            write_result["instruction"] = instruction
            write_result["path"] = path_label
            write_result["step_title"] = await _step_title(ctx, node, write_result)
        return write_result
