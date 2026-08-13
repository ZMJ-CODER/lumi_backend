"""技能插件（network/网络与web工具）：web_search —— 联网搜索（Tavily）."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.services.web_search import web_search


class WebSearchSkill(Skill):
    name = "web_search"
    description = (
        "搜索互联网获取实时信息。当用户问题涉及最新新闻、实时数据、当前事件、"
        "或本地知识库无法覆盖的信息时使用。返回带来源的搜索结果。"
    )
    category = "network"
    environment = "server"
    scenes = ["chat", "office", "game"]
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
