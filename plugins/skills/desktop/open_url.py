"""技能插件（desktop/GUI与桌面控制）：open_url —— 用默认浏览器打开网址."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class OpenUrlSkill(Skill):
    name = "open_url"
    description = (
        "用系统默认浏览器打开指定网址（网页版软件、官方站点、搜索页等）。"
        "当用户确认要打开某个网页/网页版软件时使用。"
    )
    category = "desktop"
    environment = "client"
    requires_confirmation = True
    scenes = ["chat", "office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要打开的完整网址（http/https）"},
        },
        "required": ["url"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        url = str(params.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return SkillResult(
                success=False,
                error="url 必须以 http:// 或 https:// 开头",
                error_code="INVALID_ARGS",
                retryable=False,
            )
        _notify(context, f"（正在打开网页：{url}）")
        return await run_client_skill_request(
            context.user_id if context else "",
            self.name,
            {"url": url},
            True,
        )
