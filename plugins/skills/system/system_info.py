"""技能插件（system/系统信息与硬件）：get_datetime —— 获取当前日期时间."""

from datetime import datetime, timedelta, timezone

from app.agents.skills.base import Skill, SkillContext, SkillResult


class GetDatetimeSkill(Skill):
    name = "get_datetime"
    description = (
        "获取当前日期和时间（东八区）。当用户询问今天是几号、现在几点、"
        "本周几等时间敏感问题时使用，避免依赖模型训练截止日期。"
    )
    category = "system"
    environment = "server"
    scenes = ["chat", "office", "game"]
    domain = "system"
    intent_tags = ["当前时间", "日期", "星期", "几点"]
    use_when = ["用户询问当前日期、时间或星期"]
    do_not_use_when = ["用户自己的今日待办或日程", "天气、汇率、行情等实时外部数据", "历史日期换算不必读取系统时钟"]
    selection_examples = ["“现在几点？” → 使用", "“我今天还有哪些待办？” → 不使用"]
    result_contract = "返回东八区 ISO 时间和展示文本；结果可直接用于后续日期计算。"
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
