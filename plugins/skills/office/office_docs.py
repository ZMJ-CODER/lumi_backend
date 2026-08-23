"""办公技能（office/文档编辑）：office_doc_read / office_doc_edit —— 结构化编辑可编辑办公文件."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.core.executors import run_in_compute
from app.services import office_docs


def _authorized_doc_ids(params: dict, context: SkillContext) -> list[str]:
    requested = params.get("doc_ids") or []
    if isinstance(requested, str):
        requested = [requested]
    allowed = {str(value) for value in (context.office_doc_ids or ()) if str(value)}
    # ``office_docs`` means this node's server-injected document scope, not a
    # caller-provided list. An explicit doc_ids list may only narrow that scope.
    scope = str(params.get("scope") or "office_docs").strip().lower()
    if scope not in {"office_docs", "doc_ids"}:
        return []
    selected = [str(value).strip() for value in requested if str(value).strip()] if requested else sorted(allowed)
    return [doc_id for doc_id in selected if doc_id in allowed]


class InspectDocumentSetSkill(Skill):
    name = "inspect_document_set"
    description = (
        "盘点当前任务已授权的多份办公文档，返回文件名、类型、简短摘要和页数；"
        "在多文档问题中先用它定位候选文件，再调用 read_document 读取选中的 doc_id。"
    )
    category = "office"
    environment = "server"
    scenes = ["office"]
    domain = "document"
    intent_tags = ["多文档", "定位", "盘点", "哪份", "附件", "条款"]
    use_when = ["有两份及以上已授权附件且目标文档未知", "用户问哪份文件包含某事实或条款"]
    do_not_use_when = ["目标 doc_id 已唯一明确时，用 read_document", "需要正文细节时，盘点后继续用 read_document"]
    selection_examples = ["“哪份附件写了付款期限？” → 使用"]
    result_contract = "返回可读文件卡片和候选 doc_id；候选唯一才进入 read_document。"
    handoff_to = ["read_document"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "scope": {"type": "string", "enum": ["office_docs", "doc_ids"], "description": "盘点范围：office_docs（默认，全部已授权附件）或 doc_ids（仅下方指定的已授权文件）。"},
            "doc_ids": {"type": "array", "items": {"type": "string"}, "description": "仅 scope=doc_ids 时使用；只能填写当前任务已授权的 doc_id。"},
            "query": {"type": "string", "description": "要定位的事实或条款。例如“付款期限和违约责任”。"},
        },
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        doc_ids = _authorized_doc_ids(params, context)
        if not doc_ids:
            return SkillResult(success=False, error="没有可盘点的已授权办公文档", error_code="FORBIDDEN", retryable=False)
        query = str(params.get("query") or "").strip()
        try:
            cards = await run_in_compute(office_docs.inspect_document_set, context.user_id, doc_ids, query)
        except LookupError as exc:
            return SkillResult(success=False, error=str(exc), error_code="EXEC_ERROR", retryable=False)
        selection = {
            "candidate_doc_ids": doc_ids,
            "inspected_doc_ids": [str(card.get("doc_id") or "") for card in cards],
            "query": query[:500],
            "selection_reason": "多文档事实问题先盘点授权文件，等待后续 read_document 读取具体内容",
        }
        lines = [
            f"- {card['filename']} | {card['kind']} | 页数: {card.get('page_count') or '未知'} | 摘要: {card['summary']}"
            for card in cards
        ]
        return SkillResult(success=True, output="\n".join(lines), metadata={"documents": cards, "document_selection": selection, "decision_signals": {"result_count": len(cards), "confidence_hint": {"level": "medium", "basis": ["summary_only", f"document_count={len(cards)}"]}, "more_available": False, "refine_suggestion": "根据摘要选择唯一候选；候选接近时读取多个候选核验或向用户澄清。"}})


class ReadDocumentSkill(Skill):
    """Canonical narrow reader for a document chosen by discovery."""

    name = "read_document"
    description = "读取当前任务已授权的一份办公文档。多文档问题请先用 inspect_document_set 定位，再传入选中的 doc_id。"
    category = "office"
    environment = "server"
    scenes = ["office"]
    domain = "document"
    intent_tags = ["读取", "正文", "文档", "附件", "条款"]
    use_when = ["已有唯一且已授权的 doc_id，需要读取正文/结构"]
    do_not_use_when = ["多文档且目标未知时，先用 inspect_document_set", "只需已入库知识库片段时，用 query_knowledge"]
    selection_examples = ["“读取刚定位到的合同” → 使用"]
    result_contract = "返回文件名、类型和结构化正文；供后续回答或分析使用。"
    parameters_schema = {
        "type": "object",
        "properties": {"doc_id": {"type": "string", "description": "唯一目标的已授权文档 ID；多文档场景应来自 inspect_document_set 的候选结果。"}},
        "required": ["doc_id"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        doc_id = str(params.get("doc_id") or "").strip()
        if not doc_id or doc_id not in {str(value) for value in context.office_doc_ids}:
            return SkillResult(success=False, error="文档不在当前任务的授权范围内", error_code="FORBIDDEN", retryable=False)
        try:
            await office_docs.ensure_session(context.user_id, doc_id)
            info = await run_in_compute(office_docs.read_structure, context.user_id, doc_id)
        except LookupError as exc:
            return SkillResult(success=False, error=str(exc), error_code="EXEC_ERROR", retryable=False)
        selection = {
            "selected_doc_id": doc_id,
            "selected_filename": info["filename"],
            "selection_reason": "读取 inspect_document_set 盘点后指定的文档",
        }
        return SkillResult(
            success=True,
            output=f"文档：{info['filename']}（{info['kind']}）\n\n结构：\n{info['structure'][:60000]}",
            metadata={"doc_id": doc_id, "kind": info["kind"], "filename": info["filename"], "document_selection": selection, "decision_signals": {"result_count": 1, "confidence_hint": {"level": "high", "basis": ["authorized_doc_id", "single_document_read"]}, "more_available": False}},
        )


class OfficeDocReadSkill(Skill):
    name = "office_doc_read"
    description = (
        "读取办公文档的结构（段落/表格/单元格/页面文本，而非二进制流），"
        "供分析或决定如何修改。doc_id 由上传办公文档后获得。"
    )
    category = "office"
    environment = "server"
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "doc_id": {"type": "string", "description": "办公文档会话 id"},
        },
        "required": ["doc_id"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        doc_id = str(params.get("doc_id") or "").strip()
        if not doc_id:
            return SkillResult(success=False, error="缺少 doc_id", error_code="INVALID_ARGS", retryable=False)
        try:
            await office_docs.ensure_session(context.user_id, doc_id)
            info = await run_in_compute(office_docs.read_structure, context.user_id, doc_id)
        except LookupError as exc:
            return SkillResult(success=False, error=str(exc), error_code="EXEC_ERROR", retryable=False)
        return SkillResult(
            success=True,
            output=f"文档：{info['filename']}（{info['kind']}）\n\n结构：\n{info['structure'][:60000]}",
            metadata={"doc_id": doc_id, "kind": info["kind"], "filename": info["filename"]},
        )


class OfficeDocEditSkill(Skill):
    name = "office_doc_edit"
    description = (
        "按自然语言指令编辑办公文档（docx/xlsx/pptx/md/txt/json/csv）："
        "先读结构 → 规划编辑操作 → 应用到缓冲副本，返回修改记录与修改后的结构预览；"
        "用户审核通过后才落盘。"
    )
    category = "office"
    environment = "server"
    write_op = True
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "doc_id": {"type": "string", "description": "办公文档会话 id"},
            "instruction": {"type": "string", "description": "编辑指令，如：把第1段改成… / 把所有张三改成李四"},
        },
        "required": ["doc_id", "instruction"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        doc_id = str(params.get("doc_id") or "").strip()
        instruction = str(params.get("instruction") or "").strip()
        if not doc_id or not instruction:
            return SkillResult(success=False, error="缺少 doc_id / instruction", error_code="INVALID_ARGS", retryable=False)
        try:
            await office_docs.ensure_session(context.user_id, doc_id)
            info = await run_in_compute(office_docs.read_structure, context.user_id, doc_id)
            if info["kind"] in ("pdf", "doc", "xls", "ppt", "rtf", "odt", "docm", "xlsm", "pptm"):
                return SkillResult(
                    success=False,
                    error=f"{info['kind'].upper()} 格式暂不支持结构化编辑（可读取/分析/问答）",
                    error_code="UNSUPPORTED_FORMAT",
                    retryable=False,
                )
            ops = await office_docs.plan_edits(
                instruction,
                info["structure"],
                info["kind"],
                context.user_id,
                api_key=context.llm_api_key,
            )
            records = office_docs.apply_edits(context.user_id, doc_id, ops)
            after = await run_in_compute(office_docs.read_structure, context.user_id, doc_id)
            has_failures = any(r.startswith("❌") or r.startswith("⚠️") for r in records)
        except LookupError as exc:
            return SkillResult(success=False, error=str(exc), error_code="EXEC_ERROR", retryable=False)
        return SkillResult(
            success=True,
            output=(
                ("⚠️ 未能执行有效的修改（文档未修改或部分操作失败）：\n\n" if has_failures else "✅ 修改已生成预览（未写入实际文件，等待你确认）\n\n")
                + "\n".join(records)
                + (
                    "\n\n—— 修改后结构预览 ——\n" + after["structure"][:40000]
                    if not has_failures
                    else ""
                )
            ),
            metadata={
                "doc_id": doc_id,
                "kind": after["kind"],
                "filename": after["filename"],
                "records": records,
                "committed": False,
                "pending_commit": not has_failures,
                "preview": after["structure"][:40000] if not has_failures else "",
                "ops": ops,
                "buffered": True,
            },
        )


class OfficeDocAnalyzeSkill(Skill):
    name = "office_doc_analyze"
    description = (
        "分析/问答/总结办公文档：把文档转成会话级 RAG 索引后，检索相关片段并作答。"
        "适用于'总结这个文件'、'根据文件回答：…'等分析类指令（区别于 office_doc_edit 的修改）。"
    )
    category = "office"
    environment = "server"
    scenes = ["office"]
    direct_instruction_field = "instruction"
    direct_required_fields = ["doc_id", "instruction"]
    direct_input_aliases = {"analyze_mode": "mode"}
    parameters_schema = {
        "type": "object",
        "properties": {
            "doc_id": {"type": "string", "description": "办公文档会话 id"},
            "instruction": {"type": "string", "description": "分析指令/问题"},
            "mode": {"type": "string", "description": "qa（问答，默认）或 summary（总结）"},
        },
        "required": ["doc_id", "instruction"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        doc_id = str(params.get("doc_id") or "").strip()
        instruction = str(params.get("instruction") or "").strip()
        if not doc_id or not instruction:
            return SkillResult(success=False, error="缺少 doc_id / instruction", error_code="INVALID_ARGS", retryable=False)
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
            return SkillResult(success=False, error=str(exc), error_code="EXEC_ERROR", retryable=False)
        citations = result.get("citations") or []
        meta = {
            "doc_id": doc_id,
            "citations": citations,
        }
        if mode == "summary":
            meta["mode"] = "summary"
        return SkillResult(
            success=True,
            output=result.get("answer") or "（无结果）",
            metadata=meta,
        )
