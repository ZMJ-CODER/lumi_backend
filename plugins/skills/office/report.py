"""办公技能（office/早晚报）：daily_report —— 生成早晚报内容（联网新闻 + 个人知识库）.

推送调度（定时发送）为后续迭代：本技能先生成内容，推送通道接入后复用。
"""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.registry import SkillRegistry
from app.services.office_skill_utils import office_llm


def _skill(name: str):
    return SkillRegistry.get(name)


class DailyReportSkill(Skill):
    name = "daily_report"
    description = "生成早晚报：结合当天要闻（联网搜索）与个人知识库/待办关注点，输出结构化早报或晚报"
    category = "office"
    environment = "server"
    scenes = ["office", "chat"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "period": {"type": "string", "description": "morning（早报）或 evening（晚报）"},
            "focus": {"type": "string", "description": "关注领域/关键词，逗号分隔（可空）"},
        },
        "required": ["period"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        period = str(params.get("period") or "morning").strip().lower()
        focus = str(params.get("focus") or "").strip()
        search = _skill("web_search")
        news = []
        queries = [focus] if focus else ["今日要闻 科技 财经"]
        for q in queries[:2]:
            r = await search.execute({"query": q, "max_results": 5}, context)
            if r.success:
                news.append(r.output)
        kb = ""
        if context and context.user_id:
            k = await _skill("query_knowledge").execute(
                {"query": (focus or "今日重点"), "top_k": 3}, context
            )
            if k.success:
                kb = k.output
        news_txt = "\n\n".join(news)[:50000] or "（联网搜索暂无结果）"
        period_cn = "早报" if period == "morning" else "晚报"
        out = await office_llm(
            context,
            "你是个人助理。生成一份结构化" + period_cn + "：①今日/今日回顾要闻（3-5 条，带来源）"
            "②与你关注领域相关的动态 ③个人知识库相关提醒 ④一句总结建议。引用来源时给出链接。",
            f"关注领域：{focus or '通用'}\n\n新闻材料：\n{news_txt}\n\n知识库相关：\n{kb[:20000]}",
            max_tokens=8000,
        )
        return SkillResult(success=True, output=out)
