"""代码定位/阅读 agent：定位相关文件并读取内容，输出代码上下文供下游使用."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from loguru import logger

from app.agents.core.base import WorkerAgent, WorkerContext
from app.agents.core.progress import set_progress as _report_progress
from app.agents.core.tools import _read_with_retry, locate_from_memory, locate_project_file

if TYPE_CHECKING:
    from app.agents.orchestration.models import TaskNode


class CodeReaderAgent(WorkerAgent):
    """代码定位/阅读 agent：定位相关文件并读取内容，输出代码上下文供下游使用."""

    name = "code_reader"
    description = "在本地代码项目里定位并读取相关文件内容，梳理代码上下文"
    params_help = (
        'params 用 {"project_id": "项目ID", "instruction": "定位/分析指令", "target_file": "可选文件路径"}'
    )
    skills = ["list_project", "read_project_file", "grep_code"]

    async def execute(self, node: TaskNode, ctx: WorkerContext) -> dict:
        project_id = str(node.params.get("project_id") or "")
        instruction = str(node.params.get("instruction") or "").strip()
        if not project_id:
            return {
                "success": False,
                "error": "缺少 project_id",
                "error_code": "INVALID_ARGS",
            }
        await _report_progress(ctx.job_id, node.id, "正在定位相关代码文件…")
        # 热缓存快路径优先
        t0 = time.time()
        located = await locate_from_memory(self, ctx, project_id, instruction)
        t1 = time.time()
        if not located:
            located = await locate_project_file(
                ctx.user_id,
                project_id,
                instruction,
                node.params.get("target_file"),
                node.params.get("file_key"),
            )
        t2 = time.time()
        if not located or not (located.get("path") or located.get("file_key")):
            return {
                "success": False,
                "error": "未能在项目索引中定位相关文件",
                "error_code": "EXEC_ERROR",
            }
        await _report_progress(
            ctx.job_id, node.id, f"正在阅读 {located.get('path') or located.get('file_key')}…"
        )
        read, located = await _read_with_retry(
            self,
            ctx,
            project_id,
            instruction,
            located.get("path"),
            located.get("file_key"),
        )
        t3 = time.time()
        logger.info(
            "[Reader] 耗时 定位缓存={:.0f}ms 定位={:.0f}ms 读取={:.0f}ms | {}",
            (t1 - t0) * 1000,
            (t2 - t1) * 1000,
            (t3 - t2) * 1000,
            located.get("path") or located.get("file_key") or instruction[:20],
        )
        if not read.get("success"):
            return read
        return {
            "success": True,
            "located": located,
            "path": located.get("path") or located.get("file_key"),
            "content": read.get("content") or "",
            "step_title": (
                f"阅读 {located.get('path') or located.get('file_key')}"
                if located.get("path") or located.get("file_key")
                else "阅读代码"
            ),
        }
