"""开发者公共工作流：基于已授权文档执行问答与摘要。"""

from app.agents.skills.base import SkillContext, ToolOutput, WorkflowSkill
from app.office import docs as office_docs


class OfficeDocumentAnalysisSkill(WorkflowSkill):
    """文档分析是一个公共工作流，不是可直接暴露给模型的原子 Tool。"""

    name = "office_doc_analyze"
    description = "基于当前任务已授权的单份办公文档执行问答或摘要。"
    category = "office"
    environment = "server"
    scenes = ["office"]
    provided_goals = ["RETRIEVE", "ANALYZE"]
    provided_sources = ["ATTACHED_FILE"]
    safety_level = "READ_ONLY"
    use_when = ["已明确指定一份已授权文档，需要总结、解读或问答"]
    do_not_use_when = ["目标文档未知时，先使用 inspect_document_set", "需要修改文档时，使用 office_doc_edit"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "doc_id": {"type": "string", "description": "当前任务已授权的办公文档 ID"},
            "instruction": {"type": "string", "description": "分析指令或问题"},
            "mode": {"type": "string", "enum": ["qa", "summary"], "description": "问答或摘要，默认 qa"},
        },
        "required": ["doc_id", "instruction"],
    }

    async def run(self, params: dict, context: SkillContext, invoke_tool) -> ToolOutput:
        del invoke_tool  # 文档会话 RAG 是受控业务服务，并非另一个 Tool 的直调。
        if not context.user_id:
            return ToolOutput(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        doc_id = str(params.get("doc_id") or "").strip()
        instruction = str(params.get("instruction") or "").strip()
        if not doc_id or not instruction:
            return ToolOutput(success=False, error="缺少 doc_id / instruction", error_code="INVALID_ARGS", retryable=False)
        if doc_id not in {str(value) for value in context.office_doc_ids}:
            return ToolOutput(success=False, error="文档不在当前任务的授权范围内", error_code="FORBIDDEN", retryable=False)
        mode = str(params.get("mode") or "qa").strip().lower()
        try:
            result = await office_docs.analyze_doc(
                context.user_id,
                doc_id,
                instruction,
                mode=mode,
                api_key=context.llm_api_key,
            )
        except LookupError as exc:
            return ToolOutput(success=False, error=str(exc), error_code="EXEC_ERROR", retryable=False)
        metadata = {"doc_id": doc_id, "citations": result.get("citations") or []}
        if mode == "summary":
            metadata["mode"] = "summary"
        return ToolOutput(success=True, output=result.get("answer") or "（无结果）", metadata=metadata)
