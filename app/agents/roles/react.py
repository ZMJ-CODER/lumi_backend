"""M3 办公 ReAct Worker。"""

from __future__ import annotations

import asyncio

from app.agents.core.base import WorkerAgent, WorkerContext
from app.agents.core.progress import set_progress
from app.agents.orchestration.react_runner import OfficeReactRunner


class ReactStepAgent(WorkerAgent):
    name = "react_step"
    description = "M3 动态办公步骤：根据中间结果逐轮选择单个工具并完成开放任务"
    params_help = '{"instruction":"完整任务目标"}'
    skills: list[str] = []

    async def execute(self, node, ctx: WorkerContext) -> dict:
        instruction = str(node.params.get("instruction") or node.name or "").strip()
        if not instruction:
            return {"success": False, "error": "ReAct 步骤缺少 instruction", "error_code": "INVALID_ARGS"}
        def on_progress(event):
            if isinstance(event, dict):
                status = str(event.get("status") or "执行中")
                title = str(event.get("title") or "动态步骤")
                text = f"ReAct：{title}{'已完成' if status == 'completed' else '执行中'}"
            else:
                text = str(event)
            asyncio.create_task(set_progress(ctx.job_id, node.id, text))

        result = await OfficeReactRunner(
            user_id=ctx.user_id,
            job_id=ctx.job_id,
            user_role=ctx.user_role,
            api_key=ctx.llm_api_key,
            max_rounds=int(node.params.get("max_rounds") or 6),
            on_progress=on_progress,
        ).run(instruction, office_docs=node.params.get("office_docs") or [])
        if not result.success:
            return {"success": False, "error": result.error or "ReAct 任务未完成",
                    "error_code": result.error_code or "REACT_ERROR", "retryable": False,
                    "records": result.records}
        return {"success": True, "content": result.content, "output": result.content,
                "records": result.records, "citations": result.citations,
                "step_title": "动态分析与执行"}
