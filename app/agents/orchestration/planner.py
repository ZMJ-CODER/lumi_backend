"""指挥层 Planner —— 用户意图 → 任务树（DAG）.

框架版：RulePlanner 把请求直接映射为单个检索节点，跑通链路。
后续接入 LLM 意图拆解：LangChain 结构化输出任务树，
支持"意图不明确时向用户反问"（最多 2~3 轮澄清）。
"""

import time
import uuid
from pathlib import Path
import re

from loguru import logger

from app.agents.orchestration.models import TaskNode
from app.agents.orchestration.planning.context import PlanRequestContext
from app.agents.orchestration.planning.contracts import Planner, PlannerModelError, TaskTree
from app.agents.orchestration.planning.prompting import (
    build_planner_prompt as _build_planner_prompt,
    known_agents as _known_agents,
    runtime_capability_note as _runtime_capability_note,
)
from app.agents.orchestration.intent import (
    classify,
    extract_output_contract,
    infer_new_office_document,
    resolve_direct_text_conversion,
    select_named_office_documents,
)
from app.agents.orchestration.routing_intent import (
    clarification_for_intent,
    classify_route_with_llm,
    infer_route_intent,
    merge_llm_route_intent,
    should_use_llm_route_fallback,
)
from app.agents.orchestration.planning.static_routes import compile_static_route
from app.agents.orchestration.planning.office_compound import build_text_then_todo_plan
from app.agents.orchestration.planning.read_only_dag import build_explicit_read_only_dag
from app.agents.skills.recovery import classify_model_error
from app.repositories.project_repository import (
    ProjectRepository,
    SqlAlchemyProjectRepository,
)


def _is_datetime_request(request: str) -> bool:
    """判断是否为无需 LLM 的当前日期/时间查询。

    限定为包含时间语义且带查询词的短请求，避免把“把会议时间改到明天”
    这类需要理解上下文的办公操作误判为系统时间查询。
    """
    text = (request or "").lower()
    time_terms = ("当前日期", "当前时间", "现在几点", "现在时间", "今天几号", "今天日期", "今天是几月几日")
    if not any(term in text for term in time_terms):
        return False
    # A shortcut must never consume a compound office request merely because it
    # contains a time query. Keep explanatory wording such as "用一行说明",
    # but send any independent file/system action through the normal planner.
    other_action_terms = (
        "转换", "转为", "转成", "导出", "保存", "总结", "分析", "读取", "生成", "写", "发送",
        "打开", "检索", "查找", "整理", "修改", "创建", "删除", "运行", "执行",
    )
    return not any(term in text for term in other_action_terms)


def _deterministic_read_tool_tree(request: str) -> TaskTree | None:
    """编译常见只读工具请求，避免短查询依赖规划模型。"""
    text = (request or "").strip()
    lower = text.casefold()
    if not text:
        return None
    # 快捷只读节点绝不能吞掉同一句中的外部动作。此类请求需要保留动作
    # 顺序并进入动态编排，后续由审批门控制真正的外部调用。
    external_markers = ("打开", "启动", "发送", "发邮件", "修改", "删除", "执行", "运行")
    if any(marker in lower for marker in external_markers):
        return None
    # 计算器：保留表达式原文，由 Skill 自己做安全解析；不执行任意代码。
    if any(marker in lower for marker in ("计算", "算一下", "帮我算", "算出")):
        expression = text
        node_id = f"c{int(time.time())}-{uuid.uuid4().hex[:6]}"
        return TaskTree(nodes=[TaskNode(
            id=node_id,
            name="精确计算",
            agent="atomic_step",
            params={"instruction": text, "preferred_tool": "calculator", "fallback_tools": [], "inputs": {"expression": expression}},
            depends_on=[],
        )], plan_text="调用计算器完成精确计算。")
    # 天气、新闻、汇率等公开实时信息固定走 web_research；不把“查询”本身
    # 当成联网授权，避免普通知识问题产生外部请求。
    if any(marker in lower for marker in ("天气", "新闻", "汇率", "行情", "股价", "网页", "网上", "联网", "公开资料")):
        node_id = f"w{int(time.time())}-{uuid.uuid4().hex[:6]}"
        return TaskTree(nodes=[TaskNode(
            id=node_id,
            name="联网查询",
            agent="web_research",
            params={"query": text, "top_k": 5},
            depends_on=[],
        )], plan_text="按请求查询公开实时信息。")
    # 知识库/资料类查询走受控 retrieval；没有来源词时仍只对明确查询动词
    # 生效，裸“查询”交给澄清逻辑。
    if any(marker in lower for marker in ("知识库", "资料库", "资料中", "文档中", "文件中", "根据我的资料", "根据文档", "根据文件")):
        node_id = f"k{int(time.time())}-{uuid.uuid4().hex[:6]}"
        return TaskTree(nodes=[TaskNode(
            id=node_id,
            name="知识库检索",
            agent="retrieval",
            params={"query": text, "top_k": 5},
            depends_on=[],
        )], plan_text="检索已授权知识库资料。")
    return None


def _is_multi_document_fact_request(request: str, office_docs: list[dict] | None) -> bool:
    """Use the governed feature rather than duplicating a Planner lexicon."""
    from app.agents.orchestration.policy.features import build_routing_features

    authorized = [item for item in office_docs or [] if item.get("doc_id")]
    return build_routing_features(
        request,
        has_authorized_documents=bool(authorized),
        office_document_count=len(authorized),
        office_documents=authorized,
    ).requires_multi_document_targeting


def _multi_document_targeting_tree(request: str, office_docs: list[dict]) -> TaskTree:
    """Fixed inspect → scoped read → answer path; no default ReAct loop."""
    target_id = f"md{int(time.time())}-{uuid.uuid4().hex[:6]}"
    answer_id = f"ma{int(time.time())}-{uuid.uuid4().hex[:6]}"
    docs = [
        {"doc_id": str(item.get("doc_id")), "filename": str(item.get("filename") or "")}
        for item in office_docs if item.get("doc_id")
    ]
    return TaskTree(
        nodes=[
            TaskNode(
                id=target_id,
                name="定位并读取相关文档",
                agent="document_targeting",
                params={"query": request, "office_docs": docs},
                metadata={
                    "route_channel": "rag",
                    "document_discovery_required": True,
                    "routing": {"reason": "multi_document_fact_lookup", "strategy": "fixed_targeting"},
                },
                depends_on=[],
            ),
            TaskNode(
                id=answer_id,
                name="基于定位文档回答",
                agent="direct_llm",
                params={"instruction": "仅依据前序已定位并读取的文档回答用户问题；资料不足时明确说明，不能猜测。\n用户问题：" + request},
                metadata={"route_channel": "direct_llm", "routing": {"reason": "scoped_document_answer"}},
                depends_on=[target_id],
            ),
        ],
        plan_text="先盘点摘要并唯一定位文档；仅在定位明确后读取该文档并作答。摘要无法唯一定位时，升级为受限动态核验。",
    )


def _multi_document_react_tree(request: str, office_docs: list[dict]) -> TaskTree:
    """Bounded fallback used only when summary-level selection is ambiguous."""
    return TaskTree(
        nodes=[TaskNode(
            id=f"mr{int(time.time())}-{uuid.uuid4().hex[:6]}",
            name="动态核验多份文档",
            agent="react_step",
            params={
                "instruction": request,
                "max_rounds": 4,
                "office_docs": [
                    {"doc_id": str(item.get("doc_id")), "filename": str(item.get("filename") or "")}
                    for item in office_docs if item.get("doc_id")
                ],
            },
            metadata={"route_channel": "agent", "document_discovery_required": True, "routing": {"reason": "multi_document_ambiguous_fallback"}},
        )],
        plan_text="摘要无法唯一定位目标，使用最多四轮的受限动态核验。",
    )


def _direct_conversion_tree(request: str, conversion: dict) -> TaskTree:
    """为单文件文本转换创建唯一的脚本节点。

    这里刻意不增加“先分析文件”的节点：转换操作只需原始文件字节，读取正文再让
    模型判断既无助于结果，也会让每份附件都触发一次模型和工具调用。
    """
    filename = str(conversion["filename"])
    target_extension = str(conversion["target_extension"])
    target_name = Path(str(conversion.get("output_filename") or "")).name
    if not target_name:
        target_name = f"{filename.rsplit('.', 1)[0]}{target_extension}"
    conversion_params = {
        "source_filename": filename,
        "target_extension": target_extension,
        "output_filename": target_name,
    }
    if conversion.get("text_delimiter"):
        conversion_params["text_delimiter"] = conversion["text_delimiter"]
    if conversion.get("encoding"):
        conversion_params["encoding"] = conversion["encoding"]
    return TaskTree(
        nodes=[
            TaskNode(
                id=f"s{int(time.time())}-{uuid.uuid4().hex[:6]}",
                name=f"转换文件 {filename} 为 {target_extension.lstrip('.')}",
                agent="office_script",
                params={
                    "task": request,
                    "doc_ids": [str(conversion["doc_id"])],
                    "conversion": conversion_params,
                    "output_contract": extract_output_contract(request, conversion_params),
                },
                depends_on=[],
            )
        ],
        plan_text=f"将《{filename}》转换为《{target_name}》。",
    )


def _new_office_document_tree(request: str, document: dict, office_docs: list[dict] | None = None) -> TaskTree:
    """Create one specialized node for a newly authored Office file."""
    filename = Path(str(document["filename"])).name
    document_format = str(document["format"])
    doc_ids = [str(item.get("doc_id")) for item in office_docs or [] if item.get("doc_id")]
    return TaskTree(
        nodes=[
            TaskNode(
                id=f"d{int(time.time())}-{uuid.uuid4().hex[:6]}",
                name=f"生成 {document_format.upper()} 文件 {filename}",
                agent="office_document",
                params={
                    "task": request,
                    "format": document_format,
                    "filename": filename,
                    "doc_ids": doc_ids,
                    "output_contract": {
                        "version": 1,
                        "requires_artifact": True,
                        "expected_output_names": [filename],
                        "target_extension": f".{document_format}",
                    },
                },
                depends_on=[],
            )
        ],
        plan_text=f"根据要求生成并校验《{filename}》。",
    )


def _apply_output_contract(nodes: list[TaskNode], request: str) -> None:
    """Attach the same compiler-produced contract to every file-producing node.

    The planner may be rule, template, or LLM driven.  This post-processing step
    keeps compliance requirements out of model discretion while leaving nodes
    that do not produce files untouched.
    """
    for node in nodes:
        if node.agent not in {"office_script", "office_document"}:
            continue
        if node.agent == "office_document" and node.params.get("output_contract"):
            # The new-document route has already compiled a filename even when
            # the user did not spell one out.  Do not erase that contract.
            continue
        conversion = node.params.get("conversion")
        node.params["output_contract"] = extract_output_contract(
            request, conversion if isinstance(conversion, dict) else None
        )


class RulePlanner(Planner):
    """框架版规划器：检索类请求 → 单个 retrieval 节点.

    后续：
      - LLM 意图拆解（function calling → 任务树 JSON）
      - 多节点 DAG（检索 → 分析 → 文档产出）
      - 意图不明确时反问用户（限 2~3 轮）
    """

    def __init__(self, project_repository: ProjectRepository | None = None):
        self._project_repository = project_repository or SqlAlchemyProjectRepository()

    async def plan(
        self,
        user_id: str,
        request: str,
        scene: str = "office",
        project_id: str | None = None,
        project_ids: list[str] | None = None,
        llm_api_key: str | None = None,
        clarification_answer: str | None = None,
        office_docs: list[dict] | None = None,
        prior_summaries: str = "",
        *,
        llm_config: dict | None = None,
    ) -> TaskTree:
        # 代码任务：显式指定 project_id，或在请求+澄清回答中匹配到已注册项目名
        combined = request + (f" {clarification_answer}" if clarification_answer else "")
        # 这类请求不需要由模型规划，也不能在模型凭据不可用时错误降级为知识库检索。
        # get_datetime 是只读且无副作用的系统 Skill，直接使用原始问题作参数，
        # 让执行器按用户所选时区/格式返回结果。
        if _is_datetime_request(combined):
            return TaskTree(
                nodes=[
                    TaskNode(
                        id=f"t{int(time.time())}-{uuid.uuid4().hex[:6]}",
                        name="查询日期和时间",
                        agent="atomic_step",
                        params={
                            "instruction": request,
                            "preferred_tool": "get_datetime",
                            "fallback_tools": [],
                            "inputs": {},
                        },
                        depends_on=[],
                    )
                ],
                plan_text="查询当前日期和时间。",
            )
        deterministic = _deterministic_read_tool_tree(combined)
        if deterministic is not None:
            return deterministic
        compound_plan = build_text_then_todo_plan(request)
        if compound_plan is not None:
            return TaskTree(nodes=compound_plan.nodes, plan_text=compound_plan.plan_text)
        conversion = resolve_direct_text_conversion(request, office_docs)
        if conversion:
            return _direct_conversion_tree(request, conversion)
        selected_docs, unresolved_docs, has_named_docs = select_named_office_documents(request, office_docs)
        if has_named_docs and unresolved_docs:
            return TaskTree(
                nodes=[],
                clarification=f"未能唯一定位文件《{'、'.join(unresolved_docs)}》。请从已上传文件中确认准确名称后再试。",
            )
        if has_named_docs:
            office_docs = selected_docs
        new_document = infer_new_office_document(request)
        # 已明确引用输入附件时，“生成一个新 Excel/PPT”通常是基于输入加工，
        # 不能被误判为从零创作文档；留给脚本/动态规划保留分析和交付约束。
        attachment_context = bool(
            office_docs
            and re.search(r"(?:这份|这些|附件|上传|材料|数据|文档|文件|表格)", request or "")
        )
        if new_document and not attachment_context:
            from app.agents.core.registry import AgentRegistry

            if AgentRegistry.get("office_document") is not None:
                return _new_office_document_tree(request, new_document, office_docs if has_named_docs else [])
        output_contract = extract_output_contract(request)
        # An explicit output filename turns this into an artifact-delivery task,
        # not a conversational answer. Route it through the sandbox even when
        # the wording did not happen to match a legacy "script" keyword.
        if output_contract.get("requires_artifact"):
            from app.agents.core.registry import AgentRegistry

            if AgentRegistry.get("office_script") is not None:
                return TaskTree(
                    nodes=[TaskNode(
                        id=f"s{int(time.time())}-{uuid.uuid4().hex[:6]}",
                        name="生成并校验文件",
                        agent="office_script",
                        params={
                            "task": request,
                            "doc_ids": [str(d.get("doc_id")) for d in office_docs or [] if d.get("doc_id")],
                            "output_contract": output_contract,
                        },
                        depends_on=[],
                    )],
                    plan_text=f"按要求生成并校验《{output_contract['expected_output_names'][0]}》。",
                )
        # 脚本任务（规则兜底）：带文档 + 转换/导出/批量/脚本关键词 → office_script
        if office_docs:
            from app.agents.orchestration.intent import classify

            if classify(request, office_docs).get("task_type") == "script":
                from app.agents.core.registry import AgentRegistry

                if AgentRegistry.get("office_script") is not None:
                    doc_ids = [str(d.get("doc_id")) for d in office_docs if d.get("doc_id")]
                    if doc_ids:
                        return TaskTree(
                            nodes=[
                                TaskNode(
                                    id=f"s{int(time.time())}-{uuid.uuid4().hex[:6]}",
                                    name="脚本处理文档",
                                    agent="office_script",
                                    params={
                                        "task": request,
                                        "doc_ids": doc_ids,
                                        "output_contract": extract_output_contract(request),
                                    },
                                    depends_on=[],
                                )
                            ],
                            plan_text=f"按脚本任务执行：{request}",
                        )
        # 带文档时仍继续进入模板/模式/自由规划，允许“先读文档再写邮件、建待办”等
        # 真正的多步骤 DAG；文档遗漏由下方确定性兜底节点补齐。
        resolved_project = (
            project_id
            or (project_ids[0] if project_ids else None)
            or await self._match_project(user_id, combined)
        )
        if resolved_project:
            # 代码 agent 被屏蔽（AGENT_DISABLED）时，不创建必失败的 code 节点
            from app.agents.core.registry import AgentRegistry

            if AgentRegistry.get("code") is None:
                return TaskTree(
                    nodes=[],
                    clarification="代码编写功能已停用，请改用普通对话，或试试知识库检索 / 联网搜索",
                )
            node = TaskNode(
                id=f"c{int(time.time())}-{uuid.uuid4().hex[:6]}",
                name="代码任务",
                agent="code",
                params={"project_id": resolved_project, "instruction": request},
                depends_on=[],
            )
            return TaskTree(nodes=[node])

        # 通用意图路由是确定性规划的最后一道边界。旧的办公快捷路径已经在
        # 上面处理；这里不再把所有未知请求伪装成 retrieval，而是按来源和
        # 副作用选择执行形态，低置信度请求直接向用户补问。
        if _is_multi_document_fact_request(request, office_docs):
            from app.agents.core.registry import AgentRegistry, ensure_registered

            ensure_registered()
            if "DOCUMENT_SELECTION_AMBIGUOUS" in prior_summaries and AgentRegistry.get("react_step") is not None:
                return _multi_document_react_tree(request, office_docs or [])
            if AgentRegistry.get("document_targeting") is not None and AgentRegistry.get("direct_llm") is not None:
                return _multi_document_targeting_tree(request, office_docs or [])
        route = infer_route_intent(request, office_docs, prior_summaries=prior_summaries)
        # Rules remain the fast path. A small, strict JSON classifier only fills
        # long-tail gaps; its candidate is merged and still goes through the
        # existing clarification, permission, approval, and compiler gates.
        # The classifier resolves request BYOK first and configured office /
        # global credentials second, so absence of a request-level key must not
        # disable multilingual fallback by itself.
        if should_use_llm_route_fallback(route, request):
            candidate = await classify_route_with_llm(
                request,
                user_id=user_id,
                api_key=llm_api_key,
                prior_summaries=prior_summaries,
                office_docs=office_docs,
                llm_config=llm_config,
            )
            if candidate:
                route = merge_llm_route_intent(route, candidate, request)
        # An L3 hint is not a calibrated probability. Typed side-effect
        # requests may proceed only through the existing approval gate; an
        # untyped or unresolved request still requires clarification.
        unresolved_risky = (
            route.confidence < 0.7
            and route.risk_level != "read_only"
            and (not route.objects or route.needs_clarification)
        )
        if route.needs_clarification or unresolved_risky:
            return TaskTree(
                nodes=[],
                clarification=clarification_for_intent(route, request, office_docs),
            )

        from app.agents.core.registry import AgentRegistry, ensure_registered

        ensure_registered()
        static_nodes = compile_static_route(request, route, office_docs)
        if static_nodes:
            required_agents = {node.agent for node in static_nodes}
            if all(AgentRegistry.get(name) is not None for name in required_agents):
                for static_node in static_nodes:
                    static_node.metadata.setdefault("routing", {
                        "confidence": route.confidence,
                        "classifier_confidence_hint": route.classifier_confidence_hint,
                        "confidence_detail": route.confidence_detail.__dict__ if route.confidence_detail else {},
                        "risk_level": route.risk_level,
                        "action_steps": [step.__dict__ for step in route.action_steps],
                        "resolution_notes": list(route.resolution_notes),
                    })
                return TaskTree(
                    nodes=static_nodes,
                    plan_text="按预先确定的动作顺序执行静态计划。",
                )
        node_id = f"r{int(time.time())}-{uuid.uuid4().hex[:6]}"
        if (
            route.has_multiple_actions
            or route.requires_side_effect
            or route.requires_dynamic
            or "lookup_history" in route.actions
            or "task_result" in route.objects
        ):
            if AgentRegistry.get("react_step") is None:
                return TaskTree(
                    nodes=[],
                    clarification="当前没有可用的动态执行能力，无法安全编排这个多步骤或外部操作请求。",
                )
            return TaskTree(
                nodes=[TaskNode(
                    id=node_id,
                    name="动态编排并执行",
                    agent="react_step",
                    params={
                        "instruction": route.resolved_request or request,
                        "max_rounds": 6,
                        "office_docs": [
                            {"doc_id": str(item.get("doc_id")), "filename": str(item.get("filename") or "")}
                            for item in (office_docs or [])
                            if item.get("doc_id")
                        ],
                    },
                    metadata={
                        "routing": {
                            "confidence": route.confidence,
                            "classifier_confidence_hint": route.classifier_confidence_hint,
                            "confidence_detail": route.confidence_detail.__dict__ if route.confidence_detail else {},
                            "risk_level": route.risk_level,
                            "action_steps": [step.__dict__ for step in route.action_steps],
                            "resolution_notes": list(route.resolution_notes),
                        },
                    },
                    depends_on=[],
                    approval=route.requires_side_effect,
                    approval_note="该计划包含外部状态变化，执行前需要确认。" if route.requires_side_effect else "",
                )],
                plan_text="识别多个动作后动态编排执行步骤。",
            )
        if "converse" in route.actions and not route.objects:
            if AgentRegistry.get("direct_llm") is None:
                return TaskTree(nodes=[], clarification="当前没有可用的普通对话能力，请稍后重试。")
            return TaskTree(
                nodes=[TaskNode(
                    id=node_id,
                    name="直接回答",
                    agent="direct_llm",
                    params={"instruction": request},
                    metadata={"routing": {"confidence": route.confidence, "risk_level": route.risk_level}},
                    depends_on=[],
                )],
                plan_text="这是普通对话请求，直接生成回答。",
            )
        if route.requires_network:
            if AgentRegistry.get("web_research") is None:
                return TaskTree(nodes=[], clarification="当前没有可用的联网查询能力，请稍后重试或明确提供资料来源。")
            return TaskTree(
                nodes=[TaskNode(
                    id=node_id,
                    name="联网查询",
                    agent="web_research",
                    params={"query": request, "top_k": 5},
                    depends_on=[],
                )],
                plan_text="按请求查询时效性外部信息。",
            )
        if route.requires_retrieval or "query" in route.actions:
            if AgentRegistry.get("retrieval") is None:
                return TaskTree(nodes=[], clarification="当前没有可用的知识检索能力，请稍后重试。")
            return TaskTree(
                nodes=[TaskNode(
                    id=node_id,
                    name="知识库检索",
                    agent="retrieval",
                    params={"query": request, "top_k": 5},
                    depends_on=[],
                )],
                plan_text="检索与问题相关的资料后回答。",
            )
        return TaskTree(
            nodes=[],
            clarification=clarification_for_intent(route, request, office_docs),
        )

    async def _match_project(self, user_id: str, request: str) -> str | None:
        """请求中包含已注册项目名时，返回该项目 id."""
        try:
            projects = await self._project_repository.list_projects(user_id)
            for p in projects:
                name = str(p.get("name") or "") if isinstance(p, dict) else str(getattr(p, "name", "") or "")
                project_id = p.get("id") if isinstance(p, dict) else getattr(p, "id", None)
                if name and name in request:
                    return str(project_id)
        except Exception:  # noqa: BLE001
            return None
        return None


class LlmPlanner(Planner):
    """LLM 意图拆解：用户请求 → 任务树（DAG）.

    模型：与执行层同模型（用户当前选择的云端大模型，BYOK 透传临时 key）。
    任何失败（模型不支持工具 / 解析失败 / 未定位到项目）→ 回退 RulePlanner。
    """

    def __init__(
        self,
        fallback: Planner | None = None,
        project_repository: ProjectRepository | None = None,
    ):
        self._project_repository = project_repository or SqlAlchemyProjectRepository()
        self._fallback = fallback or RulePlanner(project_repository=self._project_repository)
        from app.agents.orchestration.planning.strategies import PlannerStrategies

        self._strategies = PlannerStrategies()

    # Routing can use the context-aware level entry point for the built-in
    # planner while preserving positional dispatch for third-party planners.
    supports_context_planning = True

    async def plan_for_level(
        self,
        level,
        user_id: str | None = None,
        request: str | None = None,
        scene: str = "office",
        project_id: str | None = None,
        project_ids: list[str] | None = None,
        llm_api_key: str | None = None,
        clarification_answer: str | None = None,
        office_docs: list[dict] | None = None,
        prior_summaries: str = "",
        bypass_fast_paths: bool = False,
        *,
        context: PlanRequestContext | None = None,
    ) -> TaskTree:
        """Dispatch planning by TCA level while keeping the public Planner API stable."""
        from app.agents.orchestration.tca import ComplexityLevel

        if context is None:
            context = PlanRequestContext.from_legacy_args(
                user_id or "",
                request or "",
                scene,
                project_id,
                project_ids,
                llm_api_key,
                clarification_answer,
                office_docs,
                prior_summaries,
            )
        user_id = context.user_id
        request = context.request
        scene = context.scene
        project_id = context.project_id
        project_ids = list(context.project_ids)
        llm_api_key = context.llm_api_key
        llm_config = context.llm_config
        clarification_answer = context.clarification_answer
        office_docs = [dict(item) for item in context.office_docs]
        prior_summaries = context.prior_summaries
        level = ComplexityLevel(level)
        # Keep explicit persistent actions ahead of every level-specific fast
        # path, including M1 template selection and bypass_fast_paths replans.
        # Otherwise a generic ETL/daily template can still swallow a trailing
        # "add this to my todo" clause before the normal planner is reached.
        compound_plan = build_text_then_todo_plan(request)
        if compound_plan is not None:
            return TaskTree(nodes=compound_plan.nodes, plan_text=compound_plan.plan_text)
        explicit_read_only_dag = build_explicit_read_only_dag(request)
        if explicit_read_only_dag is not None:
            return TaskTree(
                nodes=explicit_read_only_dag,
                plan_text="按用户声明的 A/B 并行、C 汇总、D 交付的只读分析 DAG 执行。",
            )
        if bypass_fast_paths:
            selected_docs, unresolved_docs, has_named_docs = select_named_office_documents(
                request, office_docs
            )
            if has_named_docs and unresolved_docs:
                return TaskTree(
                    nodes=[],
                    clarification=f"未能唯一定位文件《{'、'.join(unresolved_docs)}》。请确认准确名称后再试。",
                )
            if has_named_docs:
                office_docs = selected_docs
            projects = await self._list_projects(user_id)
            try:
                tree = await self._plan_with_llm(
                    user_id,
                    request,
                    project_id,
                    project_ids,
                    llm_api_key,
                    projects,
                    clarification_answer,
                    office_docs,
                    prior_summaries,
                    llm_config=llm_config,
                )
            except PlannerModelError as exc:
                return TaskTree(nodes=[], error=str(exc), error_code=exc.code)
            if tree is not None:
                return tree
            return TaskTree(
                nodes=[],
                error="任务升级规划未生成可执行步骤，请补充目标或稍后重试。",
                error_code="REPLAN_EMPTY",
            )
        if level == ComplexityLevel.M0:
            return await self._fallback.plan_context(context)
        if level == ComplexityLevel.M3 and scene == "office":
            route = infer_route_intent(request, office_docs, prior_summaries=prior_summaries)
            # M3 is not synonymous with ReAct. If the action types are known
            # and the deterministic planner can compile them, preserve the
            # static DAG even when the complexity assessor was conservative.
            if not route.requires_dynamic and not route.requires_side_effect:
                static_tree = await self._fallback.plan_context(context)
                if static_tree.nodes and all(node.agent != "react_step" for node in static_tree.nodes):
                    return static_tree
            return TaskTree(
                nodes=[TaskNode(
                    id=f"r{int(time.time())}-{uuid.uuid4().hex[:6]}",
                    name="动态分析与执行",
                    agent="react_step",
                    params={
                        "instruction": request,
                        "max_rounds": 6,
                        "office_docs": [
                            {"doc_id": str(item.get("doc_id")), "filename": str(item.get("filename") or "")}
                            for item in (office_docs or [])
                            if item.get("doc_id")
                        ],
                    },
                    metadata={
                        "routing": {
                            "confidence": route.confidence,
                            "classifier_confidence_hint": route.classifier_confidence_hint,
                            "confidence_detail": route.confidence_detail.__dict__ if route.confidence_detail else {},
                            "risk_level": route.risk_level,
                            "action_steps": [step.__dict__ for step in route.action_steps],
                        },
                    },
                    depends_on=[],
                    approval=route.requires_side_effect,
                    approval_note="该计划包含外部状态变化，执行前需要确认。" if route.requires_side_effect else "",
                )],
                plan_text="根据中间结果动态选择工具并完成任务。",
            )
        if level == ComplexityLevel.M1:
            if extract_output_contract(request).get("requires_artifact"):
                return await self.plan_context(context)
            intent = classify(request, office_docs)
            template = intent.get("template") if intent.get("task_type") == "template" else None
            if template:
                tree = await self._plan_with_template(
                    user_id,
                    request,
                    office_docs,
                    llm_api_key,
                    force_template=str(template),
                    prior_summaries=prior_summaries,
                    llm_config=llm_config,
                )
                if tree is not None:
                    return tree
        return await self.plan_context(context)

    async def plan(
        self,
        user_id: str,
        request: str,
        scene: str = "office",
        project_id: str | None = None,
        project_ids: list[str] | None = None,
        llm_api_key: str | None = None,
        clarification_answer: str | None = None,
        office_docs: list[dict] | None = None,
        prior_summaries: str = "",
        *,
        llm_config: dict | None = None,
    ) -> TaskTree:
        # 确定性、无副作用的系统查询不进入模型规划。这样即使用户的 BYOK
        # 临时密钥缺失，也不会把“当前几点”这类请求误转为知识库检索。
        if _is_datetime_request(request + (f" {clarification_answer}" if clarification_answer else "")):
            return await self._fallback.plan(
                user_id,
                request,
                scene,
                project_id,
                project_ids,
                llm_api_key,
                clarification_answer,
                office_docs,
                prior_summaries,
            )
        deterministic = _deterministic_read_tool_tree(request + (f" {clarification_answer}" if clarification_answer else ""))
        if deterministic is not None:
            return deterministic
        # Explicit text-generation plus todo persistence is a small known DAG.
        # Compile it before the semi-structured ETL selector so the latter
        # cannot collapse the requested write into a text-only summary node.
        compound_plan = build_text_then_todo_plan(request)
        if compound_plan is not None:
            return TaskTree(nodes=compound_plan.nodes, plan_text=compound_plan.plan_text)
        explicit_read_only_dag = build_explicit_read_only_dag(request)
        if explicit_read_only_dag is not None:
            return TaskTree(
                nodes=explicit_read_only_dag,
                plan_text="按用户声明的 A/B 并行、C 汇总、D 交付的只读分析 DAG 执行。",
            )
        # 简单“指定文件 -> 指定文本格式”不需要模型计划。先按文件名精确定位，
        # 避免自由规划把每份上传文档都塞进读/分析节点。
        conversion = resolve_direct_text_conversion(request, office_docs)
        if conversion:
            return _direct_conversion_tree(request, conversion)
        selected_docs, unresolved_docs, has_named_docs = select_named_office_documents(request, office_docs)
        if has_named_docs and unresolved_docs:
            return TaskTree(
                nodes=[],
                clarification=f"未能唯一定位文件《{'、'.join(unresolved_docs)}》。请从已上传文件中确认准确名称后再试。",
            )
        if has_named_docs:
            office_docs = selected_docs
        projects = await self._list_projects(user_id)
        try:
            tree = await self._plan_with_llm(
                user_id,
                request,
                project_id,
                project_ids,
                llm_api_key,
                projects,
                clarification_answer,
                office_docs,
                prior_summaries,
                llm_config=llm_config,
            )
        except PlannerModelError as exc:
            logger.warning("[Planner] 办公规划模型不可用: {}", exc)
            return TaskTree(nodes=[], error=str(exc), error_code=exc.code)
        if tree is not None:
            _apply_output_contract(tree.nodes, request)
            return tree
        return await self._fallback.plan(
            user_id,
            request,
            scene,
            project_id,
            project_ids,
            llm_api_key,
                clarification_answer,
                office_docs,
                prior_summaries,
                llm_config=llm_config,
            )

    async def _list_projects(self, user_id: str) -> list[dict]:
        try:
            projects = await self._project_repository.list_projects(user_id)
            normalized = []
            for project in projects:
                if isinstance(project, dict):
                    normalized.append({
                        "id": str(project.get("id") or ""),
                        "name": str(project.get("name") or ""),
                    })
                else:
                    normalized.append({
                        "id": str(getattr(project, "id", "") or ""),
                        "name": str(getattr(project, "name", "") or ""),
                    })
            return normalized
        except Exception:  # noqa: BLE001
            return []

    async def _plan_with_llm(
        self,
        user_id: str,
        request: str,
        project_id: str | None,
        project_ids: list[str] | None,
        llm_api_key: str | None,
        projects: list[dict],
        clarification_answer: str | None = None,
        office_docs: list[dict] | None = None,
        prior_summaries: str = "",
        *,
        llm_config: dict | None = None,
    ) -> TaskTree | None:
        # 只展示用户本机项目（自动定位时聚焦这些项目，避免无关项目干扰）
        if project_ids:
            pid_set = set(project_ids)
            focused = [p for p in projects if p["id"] in pid_set]
            if focused:
                projects = focused
        # 文件清单只对代码/已选项目任务有价值。办公文档、日程、联网等请求不再
        # 串行扫描每个项目的文件索引，以免扩大提示词和规划等待时间。
        request_lower = request.lower()
        needs_project_files = bool(project_id or project_ids) or any(
            marker in request_lower
            for marker in ("代码", "code", "bug", "函数", "模块", "组件", "项目")
        )
        project_files: dict[str, list[str]] = {}
        if needs_project_files:
            for p in projects:
                try:
                    project_files[p["id"]] = await self._project_repository.list_project_files(
                        user_id, p["id"], limit=30
                    )
                except Exception:  # noqa: BLE001
                    project_files[p["id"]] = []
        files_ctx = "\n".join(
            f"- {p['name']}({p['id']}): {project_files.get(p['id'], []) or '（文件索引为空）'}"
            for p in projects
        )
        context = (
            f"用户请求：{request}\n"
            f"可用本地项目：{projects if projects else '无（retrieval 任务不需要项目）'}"
            + (f"\n项目文件清单：\n{files_ctx}" if needs_project_files and projects else "")
            + (f"\n用户指定的项目 ID：{project_id}" if project_id else "")
            + (f"\n用户补充说明：{clarification_answer}" if clarification_answer else "")
            + (
                f"\n此前已完成的任务摘要（延续上下文用，简要参考即可，不要重复执行已完成步骤）：\n{prior_summaries}"
                if prior_summaries
                else ""
            )
            + (
                "\n当前办公文档："
                + "、".join(
                    f"{d.get('filename')}(doc_id={d.get('doc_id')})"
                    for d in office_docs or []
                    if d.get("doc_id")
                )
                + "（涉及分析/修改对应文档时，office_doc 节点必须带上正确的 doc_id）"
                if office_docs
                else ""
            )
        )
        output_contract = extract_output_contract(request)
        new_document = infer_new_office_document(request)
        if new_document:
            from app.agents.core.registry import AgentRegistry

            if AgentRegistry.get("office_document") is not None:
                return _new_office_document_tree(request, new_document, office_docs or [])
        if output_contract.get("requires_artifact"):
            return TaskTree(
                nodes=[TaskNode(
                    id=f"s{int(time.time())}-{uuid.uuid4().hex[:6]}",
                    name="生成并校验文件",
                    agent="office_script",
                    params={
                        "task": request,
                        "doc_ids": [str(d.get("doc_id")) for d in office_docs or [] if d.get("doc_id")],
                        "output_contract": output_contract,
                    },
                    depends_on=[],
                )],
                plan_text=f"按要求生成并校验《{output_contract['expected_output_names'][0]}》。",
            )
        if _is_multi_document_fact_request(request, office_docs):
            from app.agents.core.registry import AgentRegistry

            if "DOCUMENT_SELECTION_AMBIGUOUS" in prior_summaries and AgentRegistry.get("react_step") is not None:
                return _multi_document_react_tree(request, office_docs or [])
            if AgentRegistry.get("document_targeting") is not None and AgentRegistry.get("direct_llm") is not None:
                return _multi_document_targeting_tree(request, office_docs or [])
        # 意图分类（规则粗分类）：模板 / 半结构 / 自由
        intent = classify(request, office_docs)
        if intent["task_type"] == "template":
            templated = await self._plan_with_template(
                user_id,
                request,
                office_docs,
                llm_api_key,
                force_template=intent["template"],
                prior_summaries=prior_summaries,
                llm_config=llm_config,
            )
            if templated is not None:
                return templated
        elif intent["task_type"] == "semi_structured":
            patterned = await self._plan_with_pattern(
                user_id,
                request,
                office_docs,
                llm_api_key,
                prior_summaries=prior_summaries,
                llm_config=llm_config,
            )
            if patterned is not None:
                return patterned
        elif intent["task_type"] == "script":
            # 脚本任务：直接建 office_script 节点（写脚本处理文档，不逐步查看）
            doc_ids = [str(d.get("doc_id")) for d in office_docs or [] if d.get("doc_id")]
            if doc_ids:
                return TaskTree(
                    nodes=[
                        TaskNode(
                            id=f"s{int(time.time())}-{uuid.uuid4().hex[:6]}",
                            name="脚本处理文档",
                            agent="office_script",
                            params={
                                "task": request,
                                "doc_ids": doc_ids,
                                "output_contract": extract_output_contract(request),
                            },
                            depends_on=[],
                        )
                    ],
                    plan_text=f"按脚本任务执行：{request}",
                )
        # free / 兜底：Plan-then-Execute（LLM 自由规划，含 office_doc 兜底注入）
        try:
            # 保持此调用的四参数契约；项目里已有插件/测试会替换该方法。
            try:
                data = await self._call_structured_planner(
                    user_id,
                    request,
                    context,
                    llm_api_key,
                    llm_config=llm_config,
                )
            except TypeError as exc:
                # 兼容旧插件/测试替换的四参数规划器。
                if "llm_config" not in str(exc):
                    raise
                data = await self._call_structured_planner(
                    user_id, request, context, llm_api_key
                )
        except PlannerModelError:
            raise
        if not data:
            return None

        clarification = str(data.get("clarification") or "").strip()
        nodes: list[TaskNode] = []
        for t in data.get("tasks") or []:
            if not isinstance(t, dict):
                continue
            agent = t.get("agent")
            if agent not in _known_agents():
                continue
            params = dict(t.get("params") or {})
            if agent == "atomic_step":
                params["instruction"] = str(params.get("instruction") or t.get("name") or request)
                params["fallback_tools"] = [
                    str(name) for name in (params.get("fallback_tools") or []) if str(name).strip()
                ][:2]
            elif agent == "retrieval":
                params.setdefault("query", request)
            elif agent.startswith("code"):
                pid = params.get("project_id") or project_id
                if not pid and len(projects) == 1:
                    pid = projects[0]["id"]
                if not pid:
                    continue  # 无法定位项目 → 丢弃该节点，由回退逻辑兜底
                params["project_id"] = pid
                params["instruction"] = str(params.get("instruction") or request)
            nodes.append(
                TaskNode(
                    id=str(t.get("id") or f"n{len(nodes) + 1}"),
                    name=str(t.get("name") or agent),
                    agent=agent,
                    params=params,
                    depends_on=[str(d) for d in (t.get("depends_on") or [])],
                )
            )
        # 确定性兜底：带了办公文档但规划结果没覆盖到 → 强制补 office_doc 分析节点，
        # 保证智能体一定能读到文档（不依赖 LLM 规划的自觉）
        if office_docs and "office_doc" in _known_agents():
            covered = {
                str(n.params.get("doc_id"))
                for n in nodes
                if n.agent == "office_doc" and n.params.get("doc_id")
            }
            # A sandbox file operation reads its explicitly mounted source
            # document directly. Do not append an unrelated LLM analysis node
            # after a valid office_script plan; it adds latency and may inspect
            # the same file twice without contributing to the artifact.
            covered.update(
                str(doc_id)
                for n in nodes
                if n.agent == "office_script"
                for doc_id in (n.params.get("doc_ids") or [])
                if doc_id
            )
            for d in office_docs or []:
                doc_id = str(d.get("doc_id") or "")
                if not doc_id or doc_id in covered:
                    continue
                fname = str(d.get("filename") or doc_id[:8])
                nodes.append(
                    TaskNode(
                        id=f"od{int(time.time())}-{uuid.uuid4().hex[:6]}-{len(nodes)}",
                        name=f"分析文档 {fname}",
                        agent="office_doc",
                        params={
                            "doc_id": doc_id,
                            "instruction": request,
                            "mode": "analyze",
                            "analyze_mode": "qa",
                        },
                        depends_on=[],
                    )
                )
        _apply_output_contract(nodes, request)
        return TaskTree(
            nodes=nodes,
            clarification=clarification,
            plan_text=str(data.get("plan") or "").strip() or None,
        )

    async def _plan_with_template(
        self,
        user_id: str,
        request: str,
        office_docs: list[dict] | None,
        llm_api_key: str | None,
        force_template: str | None = None,
        prior_summaries: str = "",
        *,
        llm_config: dict | None = None,
    ) -> TaskTree | None:
        return await self._strategies.template(
            user_id=user_id,
            request=request,
            office_docs=office_docs,
            llm_api_key=llm_api_key,
            force_template=force_template,
            prior_summaries=prior_summaries,
            llm_config=llm_config,
        )

    async def _plan_with_pattern(
        self,
        user_id: str,
        request: str,
        office_docs: list[dict] | None,
        llm_api_key: str | None,
        prior_summaries: str = "",
        *,
        llm_config: dict | None = None,
    ) -> TaskTree | None:
        return await self._strategies.pattern(
            user_id=user_id,
            request=request,
            office_docs=office_docs,
            llm_api_key=llm_api_key,
            prior_summaries=prior_summaries,
            llm_config=llm_config,
        )

    async def _call_structured_planner(
        self,
        user_id: str,
        request: str,
        context: str,
        llm_api_key: str | None,
        *,
        llm_config: dict | None = None,
    ) -> dict | None:
        """走 LangChain JSON 规划调用；失败交由 RulePlanner 确定性回退。"""
        started = time.perf_counter()
        try:
            from app.agents.langchain.planning import invoke_structured_planner
            from app.agents.orchestration.cases import format_cases, get_similar_cases

            prompt = _build_planner_prompt() + await _runtime_capability_note(request, user_id) + "\n" + context
            similar = await get_similar_cases(request, 3)
            if similar:
                prompt += "\n\n" + format_cases(similar)
            output = await invoke_structured_planner(
                prompt, user_id=user_id, api_key=llm_api_key, llm_config=llm_config
            )
            return output.model_dump()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[Planner] LangChain 结构化规划不可用，交由 RulePlanner 回退: {}", exc)
            error_code, user_error = classify_model_error(exc)
            if error_code in {
                "MODEL_INSUFFICIENT_BALANCE",
                "MODEL_AUTH_ERROR",
                "MODEL_NOT_FOUND",
                "MODEL_CONFIG_ERROR",
                "MODEL_PROVIDER_UNAVAILABLE",
                "MODEL_CONNECTION_ERROR",
                "MODEL_UNAVAILABLE",
            }:
                raise PlannerModelError(error_code, user_error) from exc
            return None
        finally:
            logger.info(
                "办公规划模型耗时: duration_ms={}",
                int((time.perf_counter() - started) * 1000),
            )
