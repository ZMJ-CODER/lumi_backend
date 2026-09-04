"""需要用户参与的基础交互工具。"""

from app.agents.skills.base import SkillContext, Tool, ToolOutput
from app.agents.skills.executor import run_client_skill_request


class AskUserQuestionTool(Tool):
    name = "AskUserQuestion"
    description = "向用户提出澄清问题或给出选项；缺少关键参数时不得自行猜测。"
    category = "interaction"
    domain = "interaction"
    resource = "user"
    environment = "client"
    parameters_schema = {
        "type": "object",
        "properties": {
            "questions": {"type": "array", "items": {"type": "object"}, "minItems": 1, "maxItems": 3},
            "multiSelect": {"type": "boolean", "default": False},
        },
        "required": ["questions"],
    }
    use_when = ["任务缺少目标、偏好或审批信息"]
    do_not_use_when = ["已有完整参数时不要打断用户", "不能用来绕过权限确认"]
    result_contract = "返回用户选择或自由输入。"

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        questions = params.get("questions")
        if not isinstance(questions, list) or not questions:
            return ToolOutput(success=False, error="questions 必须是非空数组", error_code="INVALID_ARGS", retryable=False)
        if not context or not context.user_id:
            return ToolOutput(success=False, error="需要登录后使用", error_code="AUTH_REQUIRED", retryable=False)
        return await run_client_skill_request(context.user_id, "AskUserQuestion", {"questions": questions, "multiSelect": bool(params.get("multiSelect"))})
