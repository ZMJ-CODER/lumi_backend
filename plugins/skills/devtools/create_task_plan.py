"""技能插件（devtools/开发工具链）：create_task_plan —— 把需求拆解为可执行 Task DAG."""

import json

from app.agents.skills.base import Skill, SkillContext, SkillResult


class CreateTaskPlanSkill(Skill):
    name = "create_task_plan"
    description = (
        "把用户需求拆解为可执行的任务计划（Task DAG）：拆成带依赖的任务节点，"
        "每个节点指定执行 agent（retrieval / code_reader / code_writer / code_tester / code_reviewer）。"
        "当任务复杂、需要分步执行时使用。"
    )
    category = "devtools"
    environment = "server"
    requires_confirmation = False
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "request": {"type": "string", "description": "用户需求/任务描述"},
            "project_id": {"type": "string", "description": "可选：目标项目 ID（用于提供项目文件上下文）"},
        },
        "required": ["request"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        request = str(params.get("request") or "").strip()
        if not request:
            return SkillResult(success=False, error="缺少 request", error_code="INVALID_ARGS", retryable=False)
        project_id = str(params.get("project_id") or "")
        try:
            from app.agents.orchestration.planner import _build_planner_prompt, _extract_json
            from app.core.llm import LLMClient
            from app.services.usage import CATEGORY_PLAN

            context_text = f"用户请求：{request}"
            if project_id:
                from app.core.database import async_session_factory
                from app.services import project_index

                async with async_session_factory() as session:
                    files = await project_index.list_project_files(
                        session, context.user_id, project_id, limit=30
                    )
                if files:
                    context_text += "\n项目文件清单：\n" + "\n".join(f"- {f}" for f in files)
            llm = LLMClient()
            reply = await llm.chat(
                [
                    {
                        "role": "user",
                        "content": _build_planner_prompt() + "\n" + context_text,
                    }
                ],
                temperature=0.1,
                max_tokens=16000,
                usage_user_id=context.user_id,
                usage_category=CATEGORY_PLAN,
                reasoning_effort="low",
                api_key=context.llm_api_key,
            )
            data = _extract_json(reply) if reply else None
        except Exception as exc:  # noqa: BLE001
            return SkillResult(
                success=False,
                error=f"任务规划失败: {exc}",
                error_code="EXEC_ERROR",
                retryable=True,
            )
        if not data:
            return SkillResult(
                success=False,
                error="模型未返回有效任务计划",
                error_code="EXEC_ERROR",
                retryable=True,
            )
        return SkillResult(
            success=True,
            output=json.dumps(data, ensure_ascii=False, indent=2),
            metadata={
                "tasks": data.get("tasks") or [],
                "clarification": data.get("clarification") or "",
            },
        )
