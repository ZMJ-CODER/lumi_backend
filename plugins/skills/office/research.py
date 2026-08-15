"""办公技能（office/研究与问答）：文档问答 / 竞品分析 / 客服自动回复.

文档问答与客服自动回复复用现有 query_knowledge（RAG）；竞品分析复用 web_search。
"""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.registry import SkillRegistry
from app.services.office_skill_utils import office_llm


def _skill(name: str):
    """从注册表取技能实例（插件按独立模块加载，避免重复导入注册）."""
    return SkillRegistry.get(name)


def _bad(msg: str) -> SkillResult:
    return SkillResult(success=False, error=msg, error_code="INVALID_ARGS", retryable=False)


class DocumentQaSkill(Skill):
    name = "document_qa"
    description = "文档问答：基于用户知识库（上传过的文档/资料）回答问题，可给出引用来源"
    category = "office"
    environment = "server"
    scenes = ["office", "chat"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "要回答的问题"},
            "top_k": {"type": "integer", "description": "检索片段数（默认 5）", "minimum": 1, "maximum": 10},
        },
        "required": ["question"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        question = str(params.get("question") or "").strip()
        if not question:
            return _bad("缺少 question")
        top_k = int(params.get("top_k") or 5)
        rag_skill = _skill("query_knowledge")
        rag = await rag_skill.execute(
            {"query": question, "top_k": top_k}, context
        )
        if not rag.success:
            return SkillResult(
                success=False,
                error=rag.error or "知识库中未检索到相关内容",
                error_code=rag.error_code or "EXEC_ERROR",
                retryable=False,
            )
        citations = (rag.metadata or {}).get("citations") or []
        answer = await office_llm(
            context,
            "你是文档问答助手。仅依据给定的知识库片段回答；片段不足以支撑时明确说明不知道，不要编造。",
            f"问题：{question}\n\n知识库片段：\n{rag.output[:60000]}",
            max_tokens=6000,
        )
        return SkillResult(success=True, output=answer, metadata={"citations": citations})


class CompetitorAnalysisSkill(Skill):
    name = "competitor_analysis"
    description = "竞品分析：联网搜索目标产品与竞品的公开信息，从功能、价格、优劣势、市场评价等维度输出对比分析"
    category = "office"
    environment = "server"
    scenes = ["office", "chat"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "product": {"type": "string", "description": "目标产品/公司"},
            "competitors": {"type": "string", "description": "竞品列表（逗号分隔，可空=自动找主流竞品）"},
            "dimensions": {"type": "string", "description": "分析维度（默认：功能/价格/优劣势/市场评价）"},
        },
        "required": ["product"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        product = str(params.get("product") or "").strip()
        if not product:
            return _bad("缺少 product")
        competitors = str(params.get("competitors") or "").strip()
        dims = str(params.get("dimensions") or "功能/价格/优劣势/市场评价").strip()
        search = _skill("web_search")
        queries = [product + " 评测 功能 价格"]
        if competitors:
            queries.append(competitors + " 评测 功能 价格")
        else:
            queries.append(product + " 竞品 对比")
        materials = []
        for q in queries:
            r = await search.execute({"query": q, "max_results": 5}, context)
            if r.success:
                materials.append(r.output)
        if not materials:
            return SkillResult(
                success=False,
                error="联网搜索未获取到相关公开信息",
                error_code="EXEC_ERROR",
                retryable=True,
            )
        out = await office_llm(
            context,
            "你是市场调研分析师。基于给定的搜索材料做竞品对比分析，区分事实与推断，"
            "按维度输出，最后给结论与建议。材料不足的维度明确说明。",
            f"目标产品：{product}\n竞品：{competitors or '（自动识别）'}\n维度：{dims}\n\n搜索材料：\n"
            + "\n\n---\n\n".join(materials)[:80000],
            max_tokens=8000,
        )
        return SkillResult(success=True, output=out)


class CustomerServiceSkill(Skill):
    name = "customer_service"
    description = "客服自动回复：基于知识库（FAQ/产品文档/政策）与常见客诉场景，生成专业、安抚性的客服回复"
    category = "office"
    environment = "server"
    scenes = ["office", "chat"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "客户提问/投诉内容"},
            "tone": {"type": "string", "description": "语气（默认专业温和）"},
        },
        "required": ["question"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        question = str(params.get("question") or "").strip()
        if not question:
            return _bad("缺少 question")
        tone = str(params.get("tone") or "专业温和").strip()
        rag_skill = _skill("query_knowledge")
        rag = await rag_skill.execute({"query": question, "top_k": 5}, context)
        faq = rag.output if rag.success else "（知识库未命中，请基于通用客诉处理原则回复）"
        out = await office_llm(
            context,
            "你是专业客服。回复要：共情安抚 → 明确答复/解决方案 → 下一步指引；"
            "涉及无法确认的信息不要编造，可说明将由人工跟进。只输出回复正文。",
            f"客户问题：{question}\n语气：{tone}\n知识库/FAQ：\n{faq[:40000]}",
            max_tokens=4000,
        )
        return SkillResult(success=True, output=out)
