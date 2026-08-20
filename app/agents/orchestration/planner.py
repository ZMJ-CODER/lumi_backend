"""指挥层 Planner —— 用户意图 → 任务树（DAG）.

框架版：RulePlanner 把请求直接映射为单个检索节点，跑通链路。
后续接入 LLM 意图拆解：LangChain 结构化输出任务树，
支持"意图不明确时向用户反问"（最多 2~3 轮澄清）。
"""

import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path

from loguru import logger

from app.agents.orchestration.models import TaskNode
from app.agents.orchestration.intent import (
    classify,
    extract_output_contract,
    resolve_direct_text_conversion,
    select_named_office_documents,
)
from app.agents.skills.recovery import classify_model_error


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


class TaskTree:
    """规划结果：一组带依赖的任务节点."""

    def __init__(
        self,
        nodes: list[TaskNode],
        clarification: str | None = None,
        plan_text: str | None = None,
        error: str | None = None,
        error_code: str | None = None,
    ):
        self.nodes = nodes
        self.clarification = clarification
        self.plan_text = plan_text
        self.error = error
        self.error_code = error_code


class PlannerModelError(RuntimeError):
    """规划模型无法继续时的可展示错误；不得伪装成知识库检索。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


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


def _apply_output_contract(nodes: list[TaskNode], request: str) -> None:
    """Attach the same compiler-produced contract to every file-producing node.

    The planner may be rule, template, or LLM driven.  This post-processing step
    keeps compliance requirements out of model discretion while leaving nodes
    that do not produce files untouched.
    """
    for node in nodes:
        if node.agent != "office_script":
            continue
        conversion = node.params.get("conversion")
        node.params["output_contract"] = extract_output_contract(
            request, conversion if isinstance(conversion, dict) else None
        )


class Planner(ABC):
    """指挥层基类."""

    @abstractmethod
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
    ) -> TaskTree:
        ...


class RulePlanner(Planner):
    """框架版规划器：检索类请求 → 单个 retrieval 节点.

    后续：
      - LLM 意图拆解（function calling → 任务树 JSON）
      - 多节点 DAG（检索 → 分析 → 文档产出）
      - 意图不明确时反问用户（限 2~3 轮）
    """

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

        node = TaskNode(
            id=f"t{int(time.time())}-{uuid.uuid4().hex[:6]}",
            name="知识库检索",
            agent="retrieval",
            params={"query": request, "top_k": 5},
            depends_on=[],
        )
        return TaskTree(nodes=[node])

    async def _match_project(self, user_id: str, request: str) -> str | None:
        """请求中包含已注册项目名时，返回该项目 id."""
        try:
            from app.core.database import async_session_factory
            from app.services import project_index

            async with async_session_factory() as session:
                projects = await project_index.list_projects(session, user_id)
            for p in projects:
                if p.name and p.name in request:
                    return str(p.id)
        except Exception:  # noqa: BLE001
            return None
        return None


# ── LLM 意图拆解规划器 ───────────────────────────────

# 注册表为空时的兜底 agent 清单（正常情况下由 AgentRegistry 动态生成）
_FALLBACK_AGENTS = (
    "retrieval",
    "web_research",
)

_FALLBACK_AGENT_LINES = (
    "- retrieval：检索知识库/项目索引定位信息，params 用 {\"query\": \"检索词\", \"top_k\": 5}\n"
    "- code_reader：在本地代码项目里定位并读取相关文件，params 用 {\"project_id\": \"项目ID\", \"instruction\": \"定位/分析指令\", \"target_file\": \"可选文件路径\"}\n"
    "- code_writer：生成或修改本地代码文件并写回，params 用 {\"project_id\": \"项目ID\", \"instruction\": \"编码指令\", \"target_file\": \"可选文件路径\", \"original_content\": \"可选，来自 reader\"}\n"
    "- code_tester：按项目类型自动选择并运行合适的验证命令（如 npm run build / pytest -q），params 用 {\"project_id\": \"项目ID\"}，不要预设 command，由 tester 根据项目文件自行决定\n"
    "- code_reviewer：审查已有代码或改动，params 用 {\"project_id\": \"项目ID\", \"instruction\": \"审查要求\", \"target_file\": \"可选文件路径\"}\n"
    "- code：旧版单节点代码任务（定位→生成→写回），params 用 {\"project_id\": \"项目ID\", \"instruction\": \"指令\"}\n"
)


def _agent_prompt_lines() -> str:
    """从集中注册表动态生成"可用执行 agent"提示词段（新增 agent 自动出现在规划器）."""
    try:
        from app.agents.core.registry import AgentRegistry

        agents = AgentRegistry.list()
    except Exception:  # noqa: BLE001
        agents = []
    if not agents:
        return _FALLBACK_AGENT_LINES
    lines = []
    for a in sorted(agents, key=lambda x: x.name):
        extra = f"，{a.params_help}" if a.params_help else ""
        lines.append(f"- {a.name}：{a.description}{extra}")
    return "\n".join(lines)


def _build_planner_prompt() -> str:
    """构建规划器提示词（agent 清单由注册表动态生成）."""
    from app.core.agent_security import UNTRUSTED_CONTENT_RULES

    return (
        "你是任务规划器。把用户请求拆解为任务计划。\n"
        "默认使用 atomic_step，把任务拆成可独立提交、可独立失败/重试的原子步骤。"
        "每个 atomic_step 只允许一个唯一目标、最多一次 Skill/MCP 调用；需要读后再写、搜索后再总结时必须拆成多个节点。"
        "所有步骤必须按用户叙述的顺序串行执行：除第一个步骤外，每一步都 depends_on 前一步，禁止并行。"
        "必须覆盖用户请求中的每一个动作；上传文档只提供上下文，不能让文档分析任务吞掉打开应用、写文件、发邮件等独立指令。"
        "atomic_step 可以调用当前 office 场景的本地 Skill、system Skill 和 MCP 工具，"
        "但规划时必须唯一指定 preferred_tool，执行器只会向模型暴露这一个工具。"
        "params 用 {\"instruction\":\"本步骤唯一目标\",\"preferred_tool\":\"首选工具名\",\"fallback_tools\":[\"不同原理的备用工具\"],\"inputs\":{}}。"
        "为可能失败的读取、解析、转换步骤规划不同原理且当前已允许的 fallback_tools；备用工具不得与首选工具重复。"
        "涉及同一文件、文档、日历或待办的步骤必须通过 depends_on 表达逻辑顺序；"
        "执行器还会根据输入自动声明资源读写锁。\n"
        "可用执行 agent：\n"
        + _agent_prompt_lines()
        + "\n代码任务建议按文件拆分节点，便于前端逐步展示进度：\n"
        "  - 每个需要阅读/定位的文件一个 code_reader 节点（按阅读顺序设置 depends_on）；\n"
        "  - 每个需要修改的文件一个 code_writer 节点，depends_on 指向对应的 reader 节点；\n"
        "  - 需要新建文件时，code_writer 的 target_file 用新文件路径（如 src/NewPage.vue），会自动创建；\n"
        "  - 需要删除文件/临时脚本/缓存时，生成 code_writer 节点，params 加 \"action\": \"delete\"，instruction 写清要删除的文件路径；\n"
        "  - 修改已有文件时必须明确 target_file（从项目文件清单里选具体文件）；指令没指明文件时先安排 code_reader/grep 定位，禁止因为指令模糊就默认改入口文件（app.vue / main.js / index.js 等）；\n"
        "  - 所有写入完成后一个 code_tester 节点；需要时最后加 code_reviewer。\n"
        "办公任务（涉及文档/检索/文本产出时，例如上传文档后总结/改写/生成邮件/竞品分析/整理待办）：\n"
        "  - 文件转换、批量处理、清洗、合并拆分、数据导出或生成真实文件时，优先规划单个 office_script 节点，"
        "由通用 python_exec 在授权沙箱内一次完成；禁止拆成逐行读取、逐行口述或 read_file/write_file 循环；\n"
        "  - 先分析/读取相关文档：office_doc 节点，mode=analyze 或 read，params 必须带正确的 doc_id；\n"
        "  - 再按用户要求产出：邮件/公文/改写/摘要/纪要/抽取/合规用 office_text（task=email/doc/rewrite/summary/minutes/extract/compliance，instruction 写清要求）；\n"
        "  - 竞品分析/文档问答/客服回复/早晚报用 office_research（mode=competitor/document_qa/customer_service/daily_report）；待办用 office_todo；\n"
        "  - 修改文档用 office_doc（mode=edit，带 doc_id）；\n"
        "  - 产出节点 depends_on 对应的分析/读取节点，确保先读到文档再产出。\n"
        "项目很小或任务简单（如只改/建一两个小文件）时，用最少的节点：单个 code_writer 直接完成，不要过度拆分 reader/writer。\n"
        "项目定位：根据用户请求与项目文件清单自动判断涉及哪个/哪些项目，不要因为未指定项目就澄清。\n"
        "涉及多个项目时按顺序生成任务：先完成一个项目再切换下一个（不同项目用各自的 project_id）。\n"
        "严格输出 JSON（不要代码块围栏、不要任何解释）：\n"
        "{\"plan\":\"一句话/一段执行计划（给用户看）\",\"tasks\":[{\"id\":\"t1\",\"name\":\"任务名\",\"agent\":\"retrieval\",\"params\":{},\"depends_on\":[]}],\"clarification\":\"\"}\n"
        "意图不明确或缺少关键信息（如未指定哪个项目）时，tasks 留空、clarification 填需要向用户确认的问题。"
        "\n\n" + UNTRUSTED_CONTENT_RULES
    )


async def _runtime_capability_note(request: str) -> str:
    """把当前真正能执行的工具给规划模型，避免规划不可部署的方案。"""
    try:
        from app.agents.skills.executor import select_capabilities_for_request

        capabilities = await select_capabilities_for_request(request, "office")
        names = [c.name for c in capabilities]
        return "\n当前请求可用的候选 Skill（已按权限和相关性收窄）：" + ", ".join(names)
    except Exception:  # noqa: BLE001
        return ""


def _known_agents() -> tuple[str, ...]:
    """执行层 agent 白名单（跟随集中注册表，注册表为空时回退内置清单）."""
    try:
        from app.agents.core.registry import AgentRegistry

        names = tuple(AgentRegistry.names())
    except Exception:  # noqa: BLE001
        names = ()
    return names or _FALLBACK_AGENTS


class LlmPlanner(Planner):
    """LLM 意图拆解：用户请求 → 任务树（DAG）.

    模型：与执行层同模型（用户当前选择的云端大模型，BYOK 透传临时 key）。
    任何失败（模型不支持工具 / 解析失败 / 未定位到项目）→ 回退 RulePlanner。
    """

    def __init__(self, fallback: Planner | None = None):
        self._fallback = fallback or RulePlanner()

    async def plan_for_level(
        self,
        level,
        user_id: str,
        request: str,
        scene: str = "office",
        project_id: str | None = None,
        project_ids: list[str] | None = None,
        llm_api_key: str | None = None,
        clarification_answer: str | None = None,
        office_docs: list[dict] | None = None,
        prior_summaries: str = "",
        bypass_fast_paths: bool = False,
    ) -> TaskTree:
        """Dispatch planning by TCA level while keeping the public Planner API stable."""
        from app.agents.orchestration.tca import ComplexityLevel

        level = ComplexityLevel(level)
        args = (
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
            return await self._fallback.plan(*args)
        if level == ComplexityLevel.M3 and scene == "office":
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
                    depends_on=[],
                )],
                plan_text="根据中间结果动态选择工具并完成任务。",
            )
        if level == ComplexityLevel.M1:
            if extract_output_contract(request).get("requires_artifact"):
                return await self.plan(*args)
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
                )
                if tree is not None:
                    return tree
        return await self.plan(*args)

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
        )

    async def _list_projects(self, user_id: str) -> list[dict]:
        try:
            from app.core.database import async_session_factory
            from app.services import project_index

            async with async_session_factory() as session:
                projects = await project_index.list_projects(session, user_id)
            return [{"id": str(p.id), "name": p.name} for p in projects]
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
                    from app.core.database import async_session_factory
                    from app.services import project_index

                    async with async_session_factory() as session:
                        project_files[p["id"]] = await project_index.list_project_files(
                            session, user_id, p["id"], limit=30
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
            )
            if templated is not None:
                return templated
        elif intent["task_type"] == "semi_structured":
            patterned = await self._plan_with_pattern(
                user_id, request, office_docs, llm_api_key, prior_summaries=prior_summaries
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
            data = await self._call_structured_planner(user_id, request, context, llm_api_key)
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
    ) -> TaskTree | None:
        """模板优先：规则分类命中 → 规则抽参（免 LLM）→ 模板构造器生成确定性 DAG."""
        from app.agents.orchestration.templates import get_template, template_catalog_text
        from app.agents.langchain.planning import invoke_json_object

        # 模板任务：规则抽参（不调 LLM，可靠性最高）
        if force_template:
            tmpl = get_template(force_template)
            if tmpl:
                params = _template_default_params(force_template, request)
                nodes = tmpl.build(request, params, office_docs)
                if nodes:
                    logger.info("[Planner] 模板 {} 构建 DAG（规则抽参，{} 节点）", force_template, len(nodes))
                    return TaskTree(
                        nodes=[TaskNode(**n) for n in nodes],
                        plan_text=f"按模板 {force_template} 执行：{request}",
                    )
        # 未指定模板：LLM 分类 + 抽参
        doc_desc = "、".join(
            f"{d.get('filename')}(doc_id={d.get('doc_id')})"
            for d in office_docs or []
            if d.get("doc_id")
        )
        prompt = (
            "你是办公流程规划器。根据用户请求，从模板库选择最匹配的模板并抽取参数。\n"
            "可用模板：\n"
            + template_catalog_text()
            + f"\n当前上传的文档：{doc_desc or '（无）'}\n"
            + (f"\n此前已完成的任务摘要（延续上下文用）：\n{prior_summaries}\n" if prior_summaries else "")
            + "只输出 JSON（不要 Markdown 围栏、不要解释）："
            '{"template": "模板名或null", "params": {"参数名": 值}}\n'
            "没有合适模板时 template 为 null。"
        )
        try:
            data = await invoke_json_object(
                prompt,
                user_id=user_id,
                api_key=llm_api_key,
                max_tokens=2000,
            )
            if not data:
                return None
            tname = str(data.get("template") or "")
            tmpl = get_template(tname) if tname else None
            if not tmpl:
                return None
            nodes = tmpl.build(request, dict(data.get("params") or {}), office_docs)
            if not nodes:
                return None
            logger.info(
                "[Planner] 模板 {} 构建 DAG（{} 节点）", tname, len(nodes)
            )
            return TaskTree(nodes=[TaskNode(**n) for n in nodes])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Planner] 模板规划失败，回退自由规划: {}", exc)
            error_code, user_error = classify_model_error(exc)
            if error_code in {
                "MODEL_INSUFFICIENT_BALANCE",
                "MODEL_AUTH_ERROR",
                "MODEL_NOT_FOUND",
                "MODEL_CONFIG_ERROR",
            }:
                raise PlannerModelError(error_code, user_error) from exc
            return None

    async def _plan_with_pattern(
        self,
        user_id: str,
        request: str,
        office_docs: list[dict] | None,
        llm_api_key: str | None,
        prior_summaries: str = "",
    ) -> TaskTree | None:
        """半结构任务：LLM 选模式 + 填参数 → 模式构造器生成 DAG."""
        from app.agents.orchestration.patterns import build_pattern, pattern_catalog_text
        from app.agents.langchain.planning import invoke_json_object

        prompt = (
            "你是办公流程规划器。用户请求是带条件的半结构任务，请选择一个模式并抽取参数。\n"
            + pattern_catalog_text()
            + "\n只输出 JSON（不要 Markdown 围栏）：{\"pattern\": \"模式名\", \"params\": {\"参数名\": 值}}\n"
            + (f"此前已完成的任务摘要（延续上下文用）：\n{prior_summaries}\n" if prior_summaries else "")
        )
        try:
            data = await invoke_json_object(
                prompt + f"\n用户请求：{request}",
                user_id=user_id,
                api_key=llm_api_key,
                max_tokens=2000,
            )
            if not data:
                return None
            nodes = build_pattern(
                str(data.get("pattern") or ""),
                request,
                dict(data.get("params") or {}),
                office_docs,
            )
            if not nodes:
                return None
            logger.info("[Planner] 模式 {} 构建 DAG（{} 节点）", data.get("pattern"), len(nodes))
            return TaskTree(
                nodes=[TaskNode(**n) for n in nodes],
                plan_text=f"按模式 {data.get('pattern')} 执行：{request}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Planner] 模式规划失败，回退自由规划: {}", exc)
            error_code, user_error = classify_model_error(exc)
            if error_code in {
                "MODEL_INSUFFICIENT_BALANCE",
                "MODEL_AUTH_ERROR",
                "MODEL_NOT_FOUND",
                "MODEL_CONFIG_ERROR",
            }:
                raise PlannerModelError(error_code, user_error) from exc
            return None

    async def _call_structured_planner(
        self,
        user_id: str,
        request: str,
        context: str,
        llm_api_key: str | None,
    ) -> dict | None:
        """走 LangChain JSON 规划调用；失败交由 RulePlanner 确定性回退。"""
        started = time.perf_counter()
        try:
            from app.agents.langchain.planning import invoke_structured_planner
            from app.agents.orchestration.cases import format_cases, get_similar_cases

            prompt = _build_planner_prompt() + await _runtime_capability_note(request) + "\n" + context
            similar = await get_similar_cases(request, 3)
            if similar:
                prompt += "\n\n" + format_cases(similar)
            output = await invoke_structured_planner(prompt, user_id=user_id, api_key=llm_api_key)
            return output.model_dump()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[Planner] LangChain 结构化规划不可用，交由 RulePlanner 回退: {}", exc)
            error_code, user_error = classify_model_error(exc)
            if error_code in {
                "MODEL_INSUFFICIENT_BALANCE",
                "MODEL_AUTH_ERROR",
                "MODEL_NOT_FOUND",
                "MODEL_CONFIG_ERROR",
            }:
                raise PlannerModelError(error_code, user_error) from exc
            return None
        finally:
            logger.info(
                "办公规划模型耗时: duration_ms={}",
                int((time.perf_counter() - started) * 1000),
            )


def _template_default_params(template: str, request: str) -> dict:
    """模板任务的规则抽参（免 LLM）：按请求关键词推断参数."""
    if template == "document_analysis_flow":
        task = "summary" if any(k in request for k in ("总结", "摘要")) else "qa"
        return {"task": task, "mode": "summary" if task == "summary" else "qa"}
    if template == "daily_brief_flow":
        period = "evening" if any(k in request for k in ("晚报", "晚间")) else "morning"
        return {"period": period, "focus": ""}
    if template == "invoice_filter_flow":
        return {"threshold": 10000, "alert_threshold": 50000, "notify": "财务"}
    if template == "document_compare_flow":
        return {"dimensions": ""}
    if template == "document_combine_flow":
        return {"output": "summary"}
    if template == "document_translate_flow":
        for lang in ("英文", "日文", "韩文", "法文", "德文"):
            if lang in request:
                return {"target_lang": lang}
        return {"target_lang": "中文"}
    return {}
