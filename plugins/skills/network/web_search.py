"""技能插件（network/网络与web工具）：web_search —— 联网搜索（Tavily）."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.services.web_search import web_search


class WebSearchSkill(Skill):
    name = "web_search"
    description = (
        "受控只读网页检索：仅在用户明确要求联网/网页来源，或必须核实公开互联网中的"
        "新闻、政策和外部事实时使用。不得用于用户私有任务、对话历史、上传附件、知识库"
        "内容、总结改写、创作或计算；时间词、天气或价格词本身不是调用理由。返回带来源的结果。"
    )
    category = "network"
    environment = "server"
    scenes = ["chat", "office", "game"]
    domain = "research"
    intent_tags = ["联网", "网页", "公开资料", "新闻", "来源"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索查询，建议精简为关键词"},
            "max_results": {"type": "integer", "description": "返回结果条数（默认 5）", "minimum": 1, "maximum": 10},
        },
        "required": ["query"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        query = str(params.get("query") or "").strip()
        if not query:
            return SkillResult(
                success=False,
                error="缺少搜索关键词 query",
                error_code="INVALID_ARGS",
                retryable=False,
            )
        max_results = int(params.get("max_results") or 5)
        results = await web_search(query, max_results)
        if not results:
            return SkillResult(
                success=False,
                error="未检索到相关结果，请换个关键词重试",
                error_code="EXEC_ERROR",
                retryable=True,
            )
        output = "\n\n".join(
            f"[{i + 1}] {r['title']}\n{r['url']}\n{r['content'][:800]}"
            for i, r in enumerate(results)
        )
        return SkillResult(
            success=True,
            output=output,
            metadata={
                "citations": [
                    {"type": "web", "title": r["title"], "content": r["content"][:500], "source": r["url"]}
                    for r in results
                ]
            },
        )
