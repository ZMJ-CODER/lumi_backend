"""技能：get_datetime —— 获取当前日期时间（时间敏感问题用）."""

from datetime import datetime, timedelta, timezone

from app.agents.skills.base import Skill, SkillContext, SkillResult


class GetDatetimeSkill(Skill):
    name = "get_datetime"
    description = (
        "获取当前日期和时间（东八区）。当用户询问今天是几号、现在几点、"
        "本周几等时间敏感问题时使用，避免依赖模型训练截止日期。"
    )
    category = "computation"
    environment = "server"
    scenes = ["chat", "office", "game"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "format": {
                "type": "string",
                "description": "可选：date（仅日期） / time（仅时间） / datetime（默认，完整）",
                "enum": ["date", "time", "datetime"],
            }
        },
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        now = datetime.now(timezone(timedelta(hours=8)))
        fmt = str(params.get("format") or "datetime")
        if fmt == "date":
            text = now.strftime("%Y年%m月%d日 %A")
        elif fmt == "time":
            text = now.strftime("%H:%M:%S")
        else:
            text = now.strftime("%Y年%m月%d日 %A %H:%M:%S")
        return SkillResult(success=True, output=text, metadata={"datetime": now.isoformat()})
