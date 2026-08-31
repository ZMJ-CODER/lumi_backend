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
    use_when = [
        "用户明确要求联网搜索、网页来源或公开资料",
        "需要核实公开新闻、政策或外部事实",
    ]
    do_not_use_when = [
        "用户私有任务、对话历史、上传附件或知识库内容",
        "当前日期时间应使用 get_datetime",
        "通用常识且用户未要求来源时直接回答",
    ]
    selection_examples = [
        "“联网搜索本周 AI 政策并给来源” → 使用",
        "“我今天的待办还有哪些？” → 不使用",
    ]
    result_contract = "返回 URL、标题、摘录和 citation；无结果时建议收窄公开查询条件。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "公开网页检索关键词：保留用户给出的核心实体、时间和地域限定；不得翻译、扩写、替换实体或凭空补充年份。只移除“请搜索/给来源”等请求外壳。例如“最近那个 AI 监管新规”可写为“AI 监管 新规”。"},
            "max_results": {"type": "integer", "description": "必须显式填写返回条数：用户要求列 N 条时填 N；未指定时填 5；最多 10。", "minimum": 1, "maximum": 10},
        },
        "required": ["query", "max_results"],
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
                ],
                "decision_signals": {
                    "result_count": len(results),
                    "confidence_hint": {
                        "level": "medium",
                        "basis": ["public_web_snippets", f"result_count={len(results)}"],
                    },
                    "more_available": len(results) >= max_results,
                    "refine_suggestion": "若来源不够具体，请增加主体、地域或时间限定词后重试。",
                },
            },
        )
