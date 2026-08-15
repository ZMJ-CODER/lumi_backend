"""代码 agent：方案 A —— 在用户本地项目里定位 → 读取 → LLM 生成 → 写回."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.agents.core.base import WorkerAgent, WorkerContext
from app.agents.core.progress import set_progress as _report_progress
from app.agents.core.tools import (
    CODE_SYSTEM_PROMPT,
    _read_with_retry,
    _step_title,
    effort_escalate,
    effort_start_for_task,
    generate_code_content,
    list_project_files,
    locate_from_memory,
    locate_project_file,
    review_code_content,
)

if TYPE_CHECKING:
    from app.agents.orchestration.models import TaskNode


class CodeAgent(WorkerAgent):
    """代码 agent：方案 A —— 在用户本地项目里定位 → 读取 → LLM 生成 → 写回.

    文件读写走 client 技能（Electron 本地执行，路径 jail 到项目根）；
    定位用服务器端结构索引（不读代码正文）。
    """

    name = "code"
    description = "根据指令读写本地代码项目，生成/修改代码并运行测试"
    params_help = 'params 用 {"project_id": "项目ID", "instruction": "指令"}'
    skills = [
        "list_project",
        "read_project_file",
        "write_project_file",
        "delete_project_file",
        "rename_project_file",
        "grep_code",
        "run_project_command",
    ]

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

        # 1. 定位相关代码：显式指定（真实路径/file_key）或 语义检索 + 关键词兜底
        target_path = node.params.get("target_file")
        target_key = node.params.get("file_key")
        located = None
        if not target_path and not target_key:
            await _report_progress(ctx.job_id, node.id, "正在定位相关代码文件…")
            located = await self._locate(project_id, instruction, ctx)
            target_key = located.get("file_key")
            target_path = located.get("path")
        elif target_path:
            # 显式目标：走一次索引校验，区分已有文件 / 新文件
            located = await locate_project_file(
                ctx.user_id, project_id, instruction, target_path, target_key
            )
            if located.get("path") or located.get("file_key"):
                target_path = located.get("path") or target_path
                target_key = located.get("file_key") or target_key
        if not target_path and not target_key:
            return {
                "success": False,
                "error": "未能在项目索引中定位相关文件，请明确要修改的文件",
                "error_code": "EXEC_ERROR",
            }

        # 删除意图：规划器显式传 action=delete 时才删除
        if node.params.get("action") == "delete":
            await _report_progress(
                ctx.job_id, node.id, f"正在删除 {target_path or target_key}…"
            )
            del_result = await self.run_skill(
                "delete_project_file",
                {
                    "project_id": project_id,
                    "path": target_path or target_key or "",
                    "recursive": False,
                },
                ctx,
            )
            if del_result.get("success"):
                del_result["step_title"] = f"删除 {target_path or target_key}"
                del_result["deleted"] = True
            return del_result

        # 2. 读取（client 技能，本地执行；语义命中用 file_key，客户端映射真实路径）
        if located and located.get("new_file"):
            original = ""  # 新文件：跳过读取
        else:
            await _report_progress(ctx.job_id, node.id, f"正在阅读 {target_path or target_key}…")
            read, located = await _read_with_retry(
                self, ctx, project_id, instruction, target_path, target_key
            )
            if not read.get("success"):
                return read
            if located:
                target_path = located.get("path") or target_path
                target_key = located.get("file_key") or target_key
            original = str(read.get("content") or "")

        # 3. LLM 生成修改后的完整内容（附带项目文件清单，避免乱猜路径）
        project_files = await list_project_files(ctx.user_id, project_id)
        await _report_progress(ctx.job_id, node.id, f"正在生成 {target_path or target_key} 代码…")
        effort = effort_start_for_task(
            instruction, len(original or ""), not bool((original or "").strip())
        )
        new_content = await self._generate(
            ctx, instruction, target_path or target_key or "", original, project_files
        )
        if not new_content:
            return {
                "success": False,
                "error": "模型未能生成修改内容",
                "error_code": "EXEC_ERROR",
            }
        # React 自检：写前自查，不合适则重新思考再生成（最多 2 次额外尝试）
        for _attempt in range(2):
            verdict = await review_code_content(
                ctx, instruction, target_path or target_key or "", new_content
            )
            if verdict.get("approved"):
                break
            effort = effort_escalate(effort)
            hint = str(verdict.get("feedback") or "") or "；".join(
                str(i) for i in (verdict.get("issues") or [])[:3]
            )
            await _report_progress(
                ctx.job_id, node.id, f"自检未通过，重新思考生成（{hint[:40]}）"
            )
            new_content = await self._generate(
                ctx,
                instruction,
                target_path or target_key or "",
                original,
                project_files,
                reasoning_effort=effort,
            )
            if not new_content:
                return {
                    "success": False,
                    "error": "模型未能生成修改内容",
                    "error_code": "EXEC_ERROR",
                }

        # 4. 写入（client 技能，确认弹窗）；结果附带可审查内容供质检层使用
        write_params = {"project_id": project_id}
        if target_path:
            write_params["path"] = str(target_path)
        else:
            write_params["file_key"] = target_key
        await _report_progress(ctx.job_id, node.id, f"正在写入 {target_path or target_key}…")
        write_result = await self.run_skill(
            "write_project_file", {**write_params, "content": new_content}, ctx
        )
        if write_result.get("success"):
            write_result["new_content"] = new_content
            write_result["instruction"] = instruction
            write_result["path"] = target_path or target_key
            write_result["step_title"] = await _step_title(ctx, node, write_result)
        return write_result

    async def _locate(self, project_id: str, instruction: str, ctx: WorkerContext) -> dict:
        """定位相关代码：语义检索（代码向量）优先，关键词（结构索引）兜底."""
        # 热缓存快路径优先（避免每次重新向量检索）
        located = await locate_from_memory(self, ctx, project_id, instruction)
        return located or await locate_project_file(ctx.user_id, project_id, instruction)

    async def _generate(
        self,
        ctx: WorkerContext,
        instruction: str,
        path: str,
        original: str,
        project_files: list[str] | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        return await generate_code_content(
            ctx,
            instruction,
            path,
            original,
            self._SYSTEM_PROMPT,
            project_files=project_files,
            reasoning_effort=reasoning_effort,
        )
