"""技能：query_knowledge —— 检索用户知识库（RAG）."""

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
    category = "knowledge"
    environment = "server"
    scenes = ["chat", "office", "game"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索关键词或问题，建议包含关键实体"},
            "top_k": {"type": "integer", "description": "返回片段数（默认 5）", "minimum": 1, "maximum": 10},
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
            metadata={"citations": citations},
        )
