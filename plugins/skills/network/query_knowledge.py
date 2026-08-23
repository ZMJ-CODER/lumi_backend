"""技能插件（network/网络与web工具）：query_knowledge —— 检索用户知识库（RAG）.

归类说明：属于"信息检索"类能力（从知识库而非互联网获取信息），
与 web_search 同归 network 分类，便于 LLM 按"获取外部信息"意图检索。
"""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.core.database import async_session_factory
from app.services.rag.knowledge import search_user_knowledge
from app.services.scene_manager import get_scene_knowledge_tags


class QueryKnowledgeSkill(Skill):
    name = "query_knowledge"
    description = (
        "检索用户的个人/公共知识库，获取文档中的具体事实。"
        "当用户问题涉及上传过的文档、资料、知识库内容时使用。"
    )
    category = "network"
    environment = "server"
    scenes = ["chat", "office", "game"]
    domain = "research"
    intent_tags = ["知识库", "个人资料", "已上传资料", "检索", "引用"]
    use_when = ["用户明确询问已入库的个人/公共知识库资料", "需要从历史上传资料中找事实"]
    do_not_use_when = ["当前办公附件已有唯一 doc_id 时，用 read_document", "用户要求公开网页新闻或来源时，用 web_search"]
    selection_examples = ["“根据我的知识库说明报销规则” → 使用", "“联网搜索本周政策” → 不使用"]
    result_contract = "返回知识片段与 citations；空结果时建议改用更具体的实体或确认资料是否已入库。"
    direct_instruction_field = "query"
    direct_required_fields = ["query"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "知识库问题或关键词，必须包含待查实体。例如“报销制度中住宿上限”。"},
            "top_k": {"type": "integer", "description": "返回片段数：简单事实用 3，需交叉核验用 5（默认），最多 10。", "minimum": 1, "maximum": 10},
        },
        "required": ["query"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(
                success=False,
                error="游客暂不支持知识库检索",
                error_code="INVALID_ARGS",
                retryable=False,
            )
        query = str(params.get("query") or "").strip()
        if not query:
            return SkillResult(
                success=False,
                error="缺少检索关键词 query",
                error_code="INVALID_ARGS",
                retryable=False,
            )
        tags = get_scene_knowledge_tags(context.scene)
        try:
            async with async_session_factory() as session:
                rag_context, citations = await search_user_knowledge(
                    session,
                    user_id=context.user_id,
                    query=query,
                    space_tags=tags,
                    top_k=int(params.get("top_k") or 5),
                    # 代码文件走 code 类别，不混入知识检索（普通聊天/办公检索同样排除）
                    exclude_categories=["code"],
                )
        except Exception as exc:  # noqa: BLE001
            return SkillResult(
                success=False,
                error=f"知识库检索失败: {exc}",
                error_code="EXEC_ERROR",
                retryable=True,
            )
        if not rag_context:
            return SkillResult(
                success=False,
                error="知识库中未检索到相关内容",
                error_code="EXEC_ERROR",
                retryable=False,
            )
        return SkillResult(
            success=True,
            output=rag_context,
            metadata={"citations": citations, "decision_signals": {"result_count": len(citations), "confidence_hint": {"level": "high" if len(citations) >= 2 else "medium", "basis": ["knowledge_base_citations", f"citation_count={len(citations)}"]}, "more_available": False, "refine_suggestion": "可补充文档名称、条款名或业务实体后再次检索。"}},
        )
