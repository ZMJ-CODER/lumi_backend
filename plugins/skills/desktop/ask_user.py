"""技能插件（desktop/GUI与桌面控制）：ask_user —— 向用户提问，等待人工回答.

最重要的工具：当任务缺少关键信息、需要用户决策/确认、或执行前需要人工批准时使用。
前端弹出提问框（单选选项 + 自由输入），把用户的回答回传给 agent。
"""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class AskUserSkill(Skill):
    name = "ask_user"
    description = (
        "向用户提出一个问题并等待回答。"
        "当任务缺少关键信息（目标、偏好、确认项）、需要用户做选择，或执行敏感操作前需要人工批准时，必须使用本工具，"
        "不要凭空假设。可提供选项供快速选择，也允许用户自由输入。"
    )
    category = "desktop"
    environment = "client"
    requires_confirmation = False
    scenes = ["chat", "office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "要问用户的问题（清晰、具体）"},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选：给用户的候选选项（点击即答）",
            },
            "allow_custom": {"type": "boolean", "description": "是否允许自由输入（默认 true）"},
        },
        "required": ["question"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        question = str(params.get("question") or "").strip()
        if not question:
            return SkillResult(success=False, error="缺少问题 question", error_code="INVALID_ARGS", retryable=False)
        _notify(context, "（正在向你提问…）")
        return await run_client_skill_request(
            context.user_id,
            self.name,
            {
                "question": question,
                "options": list(params.get("options") or []),
                "allow_custom": bool(params.get("allow_custom", True)),
            },
            False,
        )
