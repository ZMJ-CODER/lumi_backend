"""技能插件（desktop/GUI与桌面控制）：send_email —— 打开邮件客户端起草邮件.

客户端选择规则：
  - 用户明确指定客户端（如"用 Outlook 发"）→ 用指定客户端，并把该选择保存为用户偏好；
  - 未指定 → 用已保存的偏好（若有）；
  - 都没有 → 交给系统默认邮件客户端（mailto:）。
"""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class SendEmailSkill(Skill):
    name = "send_email"
    description = (
        "打开用户电脑的邮件客户端，起草一封给指定收件人的邮件（含主题/正文/抄送）。"
        "用户提到用某个邮箱/客户端（如 Outlook、Thunderbird、Foxmail、网易邮箱大师）时，"
        "把 client 设为对应客户端名并打开它；未指定时使用默认客户端。"
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
            "client": {
                "type": "string",
                "description": (
                    "邮件客户端：outlook / thunderbird / foxmail / mailmaster / winmail 等；"
                    "用户明确说用某个邮箱/客户端时必填；缺省=系统默认"
                ),
            },
        },
        "required": ["to"],
    }

    _CLIENT_ALIASES = {
        "outlook": "outlook",
        "microsoft outlook": "outlook",
        "ms outlook": "outlook",
        "thunderbird": "thunderbird",
        "雷鸟": "thunderbird",
        "foxmail": "foxmail",
        "网易邮箱大师": "mailmaster",
        "网易邮箱": "mailmaster",
        "邮箱大师": "mailmaster",
        "mailmaster": "mailmaster",
        "winmail": "winmail",
        "windows 邮件": "winmail",
        "qq邮箱": "qqmail",
        "qq 邮箱": "qqmail",
        "qqmail": "qqmail",
        "gmail": "gmail",
    }
    # 只有这些有桌面客户端，才允许保存为默认偏好（Gmail/QQ 邮箱等网页版不持久化）
    _PERSIST_CLIENTS = {"outlook", "thunderbird", "foxmail", "mailmaster", "winmail"}

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        to = str(params.get("to") or "").strip()
        if not to:
            return SkillResult(
                success=False, error="缺少收件人 to", error_code="INVALID_ARGS", retryable=False
            )
        user_id = context.user_id if context else ""

        # 1) 用户显式指定客户端 → 规范化 + 保存为偏好（下次默认用这个）
        client = ""
        explicit = str(params.get("client") or "").strip().lower()
        if explicit:
            client = self._CLIENT_ALIASES.get(explicit, explicit)[:32]
            if user_id and client in self._PERSIST_CLIENTS:
                from app.services.user_prefs import set_email_client

                await set_email_client(user_id, client)
        else:
            # 2) 未指定 → 读已保存的偏好
            if user_id:
                from app.services.user_prefs import get_email_client

                client = await get_email_client(user_id)

        _notify(
            context,
            f"（正在打开{'「' + client + '」' if client else '默认'}邮件客户端，起草给 {to} 的邮件…）",
        )
        return await run_client_skill_request(
            user_id,
            self.name,
            {
                "to": to,
                "subject": str(params.get("subject") or ""),
                "body": str(params.get("body") or ""),
                "cc": str(params.get("cc") or ""),
                "client": client,
            },
            False,
        )
