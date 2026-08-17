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


class OfficeCalendarAgent(WorkerAgent):
    """日历日程：新增/查看/修改/删除事件、导出 ICS（可导入真实日历）、导入 ICS."""

    name = "office_calendar"
    description = (
        "个人日历管理：新增/查看/修改/删除日历事件；导出 ICS 文件"
        "（Outlook / Google Calendar / Thunderbird / 苹果日历可直接导入）；导入 ICS 日历内容"
    )
    params_help = (
        'params 用 {"action": "add/list/update/delete/export/import", '
        '"title": "…", "start": "2026-08-20 09:00", "end": "…", '
        '"item_id": "…", "content": "ICS 内容（import 用）"}'
    )
    skills = ["calendar_manager", "task_memory"]

    async def execute(self, node: "TaskNode", ctx: WorkerContext) -> dict:
        action = str(node.params.get("action") or "").strip()
        if not action:
            return {
                "success": False,
                "error": "日历任务缺少 action",
                "error_code": "INVALID_ARGS",
            }
        await _report_progress(ctx.job_id, node.id, "正在处理日历日程…")
        result = await self.run_skill(
            "calendar_manager",
            {k: v for k, v in (node.params or {}).items() if k != "task"},
            ctx,
        )
        if result.get("success"):
            result["step_title"] = "日历日程"
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


class OfficeScriptAgent(WorkerAgent):
    """办公脚本：写 Python 脚本处理上传的文档（批量/重复任务），执行并返回产物.

    适用场景：格式转换（xlsx→csv）、批量替换、数据导出、文件整理等，
    不需要逐步查看文件内容，直接写脚本一次完成。
    """

    name = "office_script"
    description = (
        "写 Python 脚本处理上传的办公文档（批量/重复任务：格式转换如 xlsx→csv、"
        "批量替换、数据导出、文件整理等）。脚本读取文档并把产物写入输出目录"
    )
    params_help = (
        'params 用 {"task": "任务描述", "doc_ids": ["doc_id", ...], "instruction": "补充要求"}'
    )
    skills = ["python_exec", "office_doc_read", "task_memory"]

    async def execute(self, node: "TaskNode", ctx: WorkerContext) -> dict:
        task = str(node.params.get("task") or node.params.get("instruction") or "").strip()
        doc_ids = [str(x) for x in (node.params.get("doc_ids") or []) if x]
        if not task:
            return {
                "success": False,
                "error": "办公脚本任务缺少 task",
                "error_code": "INVALID_ARGS",
            }
        await _report_progress(ctx.job_id, node.id, "正在编写并执行脚本…")
        try:
            from app.services import office_docs

            doc_names = []
            for doc_id in doc_ids:
                try:
                    await office_docs.ensure_session(ctx.user_id, doc_id)
                    meta = office_docs.load_session(ctx.user_id, doc_id)
                    doc_names.append(meta.get("filename") or doc_id)
                except Exception:  # noqa: BLE001
                    doc_names.append(doc_id)
            code = await self._generate_script(task, doc_names, ctx)
            if not code:
                return {
                    "success": False,
                    "error": "脚本生成失败（模型未返回有效代码）",
                    "error_code": "EXEC_ERROR",
                }
            result = await self.run_skill(
                "python_exec",
                {"code": code, "doc_ids": doc_ids, "timeout": 60},
                ctx,
            )
            if not result.get("success"):
                return result
            return {
                **result,
                "script": code[:8000],
                "step_title": "脚本执行",
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Agent:office_script] 执行失败: {}", exc)
            return {
                "success": False,
                "error": str(exc) or "脚本执行失败",
                "error_code": "EXEC_ERROR",
            }

    async def _generate_script(self, task: str, doc_names: list[str], ctx: WorkerContext) -> str:
        """两阶段生成脚本：先伪代码（逻辑层）→ 再基于伪代码写 Python 代码（实现层）.

        策略：解耦"逻辑设计"与"语法实现"——
        第一步先让模型用自然语言描述实现步骤（读取什么/怎么处理/输出什么），
        逻辑错了可以低成本修正；逻辑通了再"翻译"成代码，避免直接写代码时
        陷入语法细节反复试错。
        """
        from app.core.llm import LLMClient
        from app.services.usage import CATEGORY_PLAN

        llm = LLMClient()
        doc_line = "、".join(doc_names) or "（无）"

        # ── 第一步：逻辑层（伪代码，禁止写代码） ──
        logic_prompt = (
            "你是办公脚本设计器。先不要写任何代码，用自然语言把任务的实现步骤描述清楚：\n"
            "1. 数据来源：读取什么（文件/环境变量里的路径）；\n"
            "2. 处理逻辑：如何转换/计算/批量处理（循环、判断、清洗）；\n"
            "3. 输出：产物文件名与写入哪个目录。\n"
            "要求步骤清晰、可执行，但要足够抽象（不涉及具体语法）。\n"
            f"任务：{task}\n"
            f"涉及文档：{doc_line}\n"
            "只输出分步逻辑描述，不要输出任何代码。"
        )
        try:
            logic = await llm.chat(
                [{"role": "user", "content": logic_prompt}],
                scene=ctx.scene,
                max_tokens=1500,
                temperature=0.2,
                usage_user_id=ctx.user_id,
                usage_category=CATEGORY_PLAN,
                disable_reasoning_effort=True,
                api_key=ctx.llm_api_key,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Agent:office_script] 伪代码生成失败: {}", exc)
            return ""
        logic = (logic or "").strip()
        if not logic:
            return ""

        # ── 第二步：实现层（基于伪代码写 Python 代码） ──
        code_prompt = (
            "你是 Python 脚本实现器。根据下面已确认的逻辑步骤，编写完整、可直接运行的 Python 代码。\n"
            "环境约定：\n"
            '- 文档通过 os.environ["LUMI_DOC_PATHS"] 读取，是 JSON（文件名→绝对路径）；\n'
            '- 产物写入 os.environ["LUMI_DOC_OUTPUT_DIRS"] 对应文件名的目录；\n'
            "- 新建文件（没有源文档时）统一写入 os.environ[\"LUMI_OUTPUT_DIR\"] 指向的目录；\n"
            "编写要求：\n"
            "- 用 os/pandas 等库，路径统一用绝对路径（从环境变量读取）；\n"
            "- 必须包含异常处理（文件不存在、空值、解析失败等），打印关键进度；\n"
            "- 可用库：openpyxl（xlsx）、python-docx（docx）、python-pptx（pptx）、csv、json、re、pathlib；\n"
            "- 所有产物文件必须写入 os.environ[\"LUMI_OUTPUT_DIR\"] 指向的目录"
            "（脚本开头用 os.makedirs(..., exist_ok=True) 确保目录存在），"
            "严禁写到当前工作目录或相对路径；\n"
            "- 产物文件名用文档原名（去扩展名）命名，如 销售数据.xlsx → 销售数据.csv；\n"
            "- 脚本必须是纯 Python 代码，能直接执行。\n"
            f"逻辑步骤：\n{logic}\n"
            f"任务：{task}\n"
            "只输出 Python 代码（不要 Markdown 围栏、不要任何解释）。"
        )
        try:
            reply = await llm.chat(
                [{"role": "user", "content": code_prompt}],
                scene=ctx.scene,
                max_tokens=5000,
                temperature=0.1,
                usage_user_id=ctx.user_id,
                usage_category=CATEGORY_PLAN,
                disable_reasoning_effort=True,
                api_key=ctx.llm_api_key,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Agent:office_script] 脚本生成调用失败: {}", exc)
            return ""
        text = (reply or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("python"):
                text = text[6:]
            elif text.startswith("py"):
                text = text[2:]
        return text.strip()


class OfficeSystemAgent(WorkerAgent):
    """办公系统操作：打开软件 / 打开文件 / 起草邮件 / 进程 / 系统信息 / 网络请求."""

    name = "office_system"
    description = (
        "打开用户电脑上的软件或文件、起草邮件、查看/结束进程、读取环境变量与时间、"
        "发起网络请求等系统操作。当用户要求打开某个软件/应用/文件、查看进程、"
        "读取系统信息、发邮件或访问某个网址时使用。"
    )
    params_help = (
        'params 用 {"task": "open_app|open_file|open_url|send_email|ps|kill|env|datetime|curl", '
        '"instruction": "指令/参数"}'
    )
    skills = ["open_app", "open_file", "open_url", "send_email", "ps", "kill", "env", "get_datetime", "curl"]

    _TASK_SKILL = {
        "open_app": "open_app",
        "open_file": "open_file",
        "open_url": "open_url",
        "send_email": "send_email",
        "ps": "ps",
        "kill": "kill",
        "env": "env",
        "datetime": "get_datetime",
        "curl": "curl",
    }

    _STEP_TITLES = {
        "open_app": "打开软件",
        "open_file": "打开文件",
        "open_url": "打开网页",
        "send_email": "起草邮件",
        "ps": "查看进程",
        "kill": "结束进程",
        "env": "读取环境变量",
        "datetime": "获取时间",
        "curl": "网络请求",
    }

    async def execute(self, node: "TaskNode", ctx: WorkerContext) -> dict:
        task = str(node.params.get("task") or "").strip().lower()
        instruction = str(node.params.get("instruction") or "").strip()
        if not task:
            # 兜底：按指令文本粗判（规划器漏标 task 时仍可执行）
            low = instruction.lower()
            if low.startswith(("http://", "https://")):
                task = "curl"
            elif "进程" in instruction or "运行" in instruction:
                task = "ps"
            else:
                task = "open_app"
        skill = self._TASK_SKILL.get(task)
        if not skill:
            return {
                "success": False,
                "error": f"不支持的 office_system task: {task}",
                "error_code": "INVALID_ARGS",
            }
        await _report_progress(ctx.job_id, node.id, "正在执行系统操作…")
        params = self._build_params(task, node.params, instruction)
        if params is None:
            return {
                "success": False,
                "error": "缺少必要参数（请提供要操作的软件/文件/URL 等）",
                "error_code": "INVALID_ARGS",
            }
        result = await self.run_skill(skill, params, ctx)
        if result.get("success"):
            result["step_title"] = self._STEP_TITLES.get(task, "系统操作")
        return result

    @staticmethod
    def _build_params(task: str, raw: dict, instruction: str) -> dict | None:
        if task == "open_app":
            name = instruction or str(raw.get("name") or "").strip()
            return {"name": name, "args": list(raw.get("args") or [])} if name else None
        if task == "open_file":
            path = instruction or str(raw.get("path") or "").strip()
            return {"path": path} if path else None
        if task == "open_url":
            url = instruction or str(raw.get("url") or "").strip()
            return {"url": url} if url else None
        if task == "send_email":
            to = str(raw.get("to") or "").strip() or instruction
            return {
                "to": to,
                "subject": str(raw.get("subject") or ""),
                "body": str(raw.get("body") or ""),
                "cc": str(raw.get("cc") or ""),
                "client": str(raw.get("client") or "").strip(),
            }
        if task == "ps":
            return {
                "pattern": str(raw.get("pattern") or instruction or "").strip(),
                "max_results": int(raw.get("max_results") or 50),
            }
        if task == "kill":
            return {
                "pid": raw.get("pid"),
                "name": str(raw.get("name") or instruction or "").strip() or None,
                "force": bool(raw.get("force", True)),
            }
        if task == "env":
            keys = raw.get("keys") or ([instruction] if instruction else [])
            return {"keys": list(keys) if isinstance(keys, list) else [str(keys)]}
        if task == "datetime":
            return {}
        if task == "curl":
            url = instruction or str(raw.get("url") or "").strip()
            return {
                "url": url,
                "method": str(raw.get("method") or "GET"),
                "headers": raw.get("headers") or {},
                "body": raw.get("body") or {},
                "timeout": int(raw.get("timeout") or 20),
            }
        return None
