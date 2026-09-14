"""办公技能（office/早晚报）：daily_report —— 生成早晚报内容（联网新闻 + 个人知识库）.

推送调度（定时发送）为后续迭代：本技能先生成内容，推送通道接入后复用。
"""

from app.agents.skills.base import WorkflowSkill, SkillContext, ToolOutput
from app.office.skill_utils import office_llm


class DailyReportSkill(WorkflowSkill):
    name = "daily_report"
    description = "生成早晚报：结合当天要闻（联网搜索）与个人知识库/待办关注点，输出结构化早报或晚报"
    category = "office"
    environment = "server"
    scenes = ["office", "chat"]
    allowed_tools = ["web_search", "query_knowledge"]
    use_when = ["用户需要把公开资讯与个人知识库内容组合成早报或晚报"]
    do_not_use_when = ["只需单次网页搜索或单次知识库检索"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "period": {"type": "string", "description": "morning（早报）或 evening（晚报）"},
            "focus": {"type": "string", "description": "关注领域/关键词，逗号分隔（可空）"},
        },
        "required": ["period"],
    }

    async def run(self, params: dict, context: SkillContext, invoke_tool) -> ToolOutput:
        period = str(params.get("period") or "morning").strip().lower()
        focus = str(params.get("focus") or "").strip()
        news = []
        queries = [focus] if focus else ["今日要闻 科技 财经"]
        for q in queries[:2]:
            r = await invoke_tool("web_search", {"query": q, "max_results": 5})
            if r.success:
                news.append(r.output)
        kb = ""
        if context and context.user_id:
            k = await invoke_tool("query_knowledge", {"query": (focus or "今日重点"), "top_k": 3})
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
        return ToolOutput(success=True, output=out)

