"""办公执行 agent：把办公技能包装为 DAG 可编排节点.

技能是原子能力（LLM 在办公对话中可直接调用）；这里是供多智能体编排使用的
agent 包装，规划器按任务类型创建对应节点。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from app.agents.core.base import WorkerAgent, WorkerContext
from app.agents.core.progress import set_progress as _report_progress

if TYPE_CHECKING:
    from app.agents.orchestration.models import TaskNode


class OfficeTextAgent(WorkerAgent):
    """办公文本任务：邮件/公文/改写/摘要/纪要/抽取/合规（按 task 路由到对应技能）."""

    name = "office_text"
    description = "办公文本任务：邮件撰写、公文撰写、多风格改写、长文摘要、会议纪要、信息抽取、合规审查"
    params_help = (
        'params 用 {"instruction": "指令", "task": "email|doc|rewrite|summary|minutes|extract|invoice|compliance"}'
    )
    skills = [
        "compose_email",
        "compose_official_doc",
        "rewrite_text",
        "summarize_text",
        "meeting_minutes",
        "extract_info",
        "invoice_parse",
        "compliance_check",
        "task_memory",
    ]

    _TASK_SKILL = {
        "email": "compose_email",
        "doc": "compose_official_doc",
        "rewrite": "rewrite_text",
        "summary": "summarize_text",
        "minutes": "meeting_minutes",
        "extract": "extract_info",
        "invoice": "invoice_parse",
        "compliance": "compliance_check",
    }

    async def execute(self, node: TaskNode, ctx: WorkerContext) -> dict:
        instruction = str(node.params.get("instruction") or "").strip()
        task = str(node.params.get("task") or "email").strip().lower()
        skill = self._TASK_SKILL.get(task)
        if not instruction:
            return {"success": False, "error": "办公文本任务缺少 instruction", "error_code": "INVALID_ARGS"}
        if not skill:
            return {
                "success": False,
                "error": f"不支持的 office_text task: {task}",
                "error_code": "INVALID_ARGS",
            }
        logger.debug("[Agent:office_text] task={} instruction={}", task, instruction[:40])
        await _report_progress(ctx.job_id, node.id, "正在处理办公文本任务…")
        params = {"instruction": instruction}
        if task == "invoice":
            params = {"text": instruction}
        result = await self.run_skill(skill, params, ctx)
        if result.get("success"):
            result["step_title"] = _STEP_TITLES.get(task, "办公文本任务")
        return result


_STEP_TITLES = {
    "email": "撰写邮件",
    "doc": "撰写公文",
    "rewrite": "多风格改写",
    "summary": "长文摘要",
    "minutes": "会议纪要整理",
    "extract": "信息抽取",
    "invoice": "发票信息提取",
    "compliance": "敏感词合规审查",
}


class OfficeResearchAgent(WorkerAgent):
    """办公研究任务：竞品分析 / 文档问答 / 客服回复 / 早晚报（联网 + 知识库）."""

    name = "office_research"
    description = "办公研究任务：竞品分析、文档问答、客服自动回复、早晚报生成"
    params_help = (
        'params 用 {"instruction": "指令/问题", "mode": "competitor|document_qa|customer_service|daily_report"}'
    )
    skills = [
        "competitor_analysis",
        "document_qa",
        "customer_service",
        "daily_report",
        "task_memory",
    ]

    _MODE_SKILL = {
        "competitor": "competitor_analysis",
        "document_qa": "document_qa",
        "customer_service": "customer_service",
        "daily_report": "daily_report",
    }

    async def execute(self, node: TaskNode, ctx: WorkerContext) -> dict:
        instruction = str(node.params.get("instruction") or "").strip()
        mode = str(node.params.get("mode") or "document_qa").strip().lower()
        skill = self._MODE_SKILL.get(mode)
        if not instruction:
            return {"success": False, "error": "办公研究任务缺少 instruction", "error_code": "INVALID_ARGS"}
        if not skill:
            return {
                "success": False,
                "error": f"不支持的 office_research mode: {mode}",
                "error_code": "INVALID_ARGS",
            }
        logger.debug("[Agent:office_research] mode={} instruction={}", mode, instruction[:40])
        await _report_progress(ctx.job_id, node.id, "正在执行办公研究任务…")
        if mode == "competitor":
            params = {"product": instruction}
        elif mode == "daily_report":
            params = {"period": str(node.params.get("period") or "morning")}
        else:
            params = {"question": instruction}
        result = await self.run_skill(skill, params, ctx)
        if result.get("success"):
            result["step_title"] = _RESEARCH_TITLES.get(mode, "办公研究")
        return result


_RESEARCH_TITLES = {
    "competitor": "竞品分析",
    "document_qa": "文档问答",
    "customer_service": "客服回复",
    "daily_report": "早晚报生成",
}


class OfficeTodoAgent(WorkerAgent):
    """日程待办：增/查/完成/删（复用 todo_manager 技能）."""

    name = "office_todo"
    description = "个人日程/待办管理：新增、查看、完成、删除待办事项"
    params_help = 'params 用 {"action": "add/list/complete/delete", "content": "…", "due": "…", "item_id": "…"}'
    skills = ["todo_manager", "task_memory"]

    async def execute(self, node: TaskNode, ctx: WorkerContext) -> dict:
        action = str(node.params.get("action") or "").strip()
        if not action:
            return {"success": False, "error": "日程待办任务缺少 action", "error_code": "INVALID_ARGS"}
        await _report_progress(ctx.job_id, node.id, "正在处理待办…")
        result = await self.run_skill(
            "todo_manager",
            {
                "action": action,
                "content": str(node.params.get("content") or ""),
                "due": str(node.params.get("due") or ""),
                "item_id": str(node.params.get("item_id") or ""),
            },
            ctx,
        )
        if result.get("success"):
            result["step_title"] = "日程待办"
        return result


class OfficeDocAgent(WorkerAgent):
    """办公文档：读取结构 / 结构化编辑（缓冲）/ 分析问答总结（会话 RAG）."""

    name = "office_doc"
    description = "办公文档处理：读取文档结构、按指令编辑（缓冲，审核后落盘）、分析/问答/总结"
    params_help = (
        'params 用 {"doc_id": "文档会话id", "instruction": "指令", "mode": "read|edit|analyze", "analyze_mode": "qa|summary"}'
    )
    skills = ["office_doc_read", "office_doc_edit", "office_doc_analyze", "task_memory"]

    async def execute(self, node: TaskNode, ctx: WorkerContext) -> dict:
        doc_id = str(node.params.get("doc_id") or "").strip()
        instruction = str(node.params.get("instruction") or "").strip()
        mode = str(node.params.get("mode") or "read").strip().lower()
        if not doc_id:
            return {"success": False, "error": "office_doc 任务缺少 doc_id", "error_code": "INVALID_ARGS"}
        await _report_progress(ctx.job_id, node.id, "正在处理办公文档…")
        if mode == "read":
            result = await self.run_skill("office_doc_read", {"doc_id": doc_id}, ctx)
        elif mode == "edit":
            if not instruction:
                return {"success": False, "error": "edit 模式需要 instruction", "error_code": "INVALID_ARGS"}
            result = await self.run_skill("office_doc_edit", {"doc_id": doc_id, "instruction": instruction}, ctx)
        elif mode == "analyze":
            if not instruction:
                return {"success": False, "error": "analyze 模式需要 instruction", "error_code": "INVALID_ARGS"}
            result = await self.run_skill(
                "office_doc_analyze",
                {
                    "doc_id": doc_id,
                    "instruction": instruction,
                    "mode": str(node.params.get("analyze_mode") or "qa"),
                },
                ctx,
            )
        else:
            return {"success": False, "error": f"不支持的 office_doc mode: {mode}", "error_code": "INVALID_ARGS"}
        if result.get("success"):
            result["step_title"] = {"read": "读取文档结构", "edit": "编辑文档", "analyze": "分析文档"}.get(mode, "办公文档")
        return result
