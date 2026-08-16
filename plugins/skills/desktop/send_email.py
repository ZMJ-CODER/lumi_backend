"""技能插件（desktop/GUI与桌面控制）：send_email —— 打开默认邮件客户端起草邮件."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class SendEmailSkill(Skill):
    name = "send_email"
    description = (
        "打开用户电脑的默认邮件客户端，起草一封给指定收件人的邮件（含主题/正文/抄送）。"
        "当用户要求发邮件、写邮件给某人时使用；实际发送由用户在自己邮箱里确认。"
    )
    category = "desktop"
    environment = "client"
    scenes = ["chat", "office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "收件人邮箱，多个用逗号分隔"},
            "subject": {"type": "string", "description": "邮件主题"},
            "body": {"type": "string", "description": "邮件正文"},
            "cc": {"type": "string", "description": "抄送邮箱，多个用逗号分隔（可选）"},
        },
        "required": ["to"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        to = str(params.get("to") or "").strip()
        if not to:
            return SkillResult(
                success=False, error="缺少收件人 to", error_code="INVALID_ARGS", retryable=False
            )
        _notify(context, f"（正在打开邮件客户端，起草给 {to} 的邮件…）")
        return await run_client_skill_request(
            context.user_id if context else "",
            self.name,
            {
                "to": to,
                "subject": str(params.get("subject") or ""),
                "body": str(params.get("body") or ""),
                "cc": str(params.get("cc") or ""),
            },
            False,
        )
