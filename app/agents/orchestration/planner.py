"""指挥层 Planner —— 用户意图 → 任务树（DAG）.

框架版：RulePlanner 把请求直接映射为单个检索节点，跑通链路。
后续接入 LLM 意图拆解：function calling 输出结构化任务树 JSON，
支持"意图不明确时向用户反问"（最多 2~3 轮澄清）。
"""

import json
import re
import time
import uuid
from abc import ABC, abstractmethod

from loguru import logger

from app.agents.orchestration.models import TaskNode
from app.agents.orchestration.intent import classify


class TaskTree:
    """规划结果：一组带依赖的任务节点."""

    def __init__(
        self,
        nodes: list[TaskNode],
        clarification: str | None = None,
        plan_text: str | None = None,
    ):
        self.nodes = nodes
        self.clarification = clarification
        self.plan_text = plan_text


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
    ) -> TaskTree:
        # 代码任务：显式指定 project_id，或在请求+澄清回答中匹配到已注册项目名
        combined = request + (f" {clarification_answer}" if clarification_answer else "")
        # 办公文档任务（规则回退）：带文档 → 直接建 office_doc 分析节点
        if office_docs:
            from app.agents.core.registry import AgentRegistry

            if AgentRegistry.get("office_doc") is not None:
                nodes = []
                for d in office_docs or []:
                    doc_id = str(d.get("doc_id") or "")
                    if not doc_id:
                        continue
                    nodes.append(
                        TaskNode(
                            id=f"od{int(time.time())}-{uuid.uuid4().hex[:6]}-{len(nodes)}",
                            name=f"分析文档 {d.get('filename') or doc_id[:8]}",
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
                if nodes:
                    return TaskTree(nodes=nodes)
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
    return (
        "你是任务规划器。把用户请求拆解为任务计划。\n"
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
    )


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
    ) -> TaskTree:
        projects = await self._list_projects(user_id)
        tree = await self._plan_with_llm(
            user_id, request, project_id, project_ids, llm_api_key, projects, clarification_answer, office_docs
        )
        if tree is not None:
            return tree
        return await self._fallback.plan(
            user_id, request, scene, project_id, project_ids, llm_api_key, clarification_answer, office_docs
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
    ) -> TaskTree | None:
        # 只展示用户本机项目（自动定位时聚焦这些项目，避免无关项目干扰）
        if project_ids:
            pid_set = set(project_ids)
            focused = [p for p in projects if p["id"] in pid_set]
            if focused:
                projects = focused
        # 为代码任务提供项目文件清单（前 30 个路径），帮助模型指定 target_file
        project_files: dict[str, list[str]] = {}
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
            + (f"\n项目文件清单：\n{files_ctx}" if projects else "")
            + (f"\n用户指定的项目 ID：{project_id}" if project_id else "")
            + (f"\n用户补充说明：{clarification_answer}" if clarification_answer else "")
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
        # 意图分类（规则粗分类）：模板 / 半结构 / 自由
        intent = classify(request, office_docs)
        if intent["task_type"] == "template":
            templated = await self._plan_with_template(
                user_id, request, office_docs, llm_api_key, force_template=intent["template"]
            )
            if templated is not None:
                return templated
        elif intent["task_type"] == "semi_structured":
            patterned = await self._plan_with_pattern(user_id, request, office_docs, llm_api_key)
            if patterned is not None:
                return patterned
        # free / 兜底：Plan-then-Execute（LLM 自由规划，含 office_doc 兜底注入）
        raw = await self._call_planner(user_id, request, context, llm_api_key)
        data = _extract_json(raw) if raw else None
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
            if agent == "retrieval":
                params.setdefault("query", request)
            elif agent in _known_agents()[1:]:
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
    ) -> TaskTree | None:
        """模板优先：规则分类命中 → 规则抽参（免 LLM）→ 模板构造器生成确定性 DAG."""
        from app.core.llm import LLMClient
        from app.services.usage import CATEGORY_PLAN
        from app.agents.orchestration.templates import get_template, template_catalog_text

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
            + "只输出 JSON（不要 Markdown 围栏、不要解释）："
            '{"template": "模板名或null", "params": {"参数名": 值}}\n'
            "没有合适模板时 template 为 null。"
        )
        try:
            llm = LLMClient()
            reply = await llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=2000,
                usage_user_id=user_id,
                usage_category=CATEGORY_PLAN,
                disable_reasoning_effort=True,
                api_key=llm_api_key,
            )
            data = _extract_json(reply)
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
            return None

    async def _plan_with_pattern(
        self,
        user_id: str,
        request: str,
        office_docs: list[dict] | None,
        llm_api_key: str | None,
    ) -> TaskTree | None:
        """半结构任务：LLM 选模式 + 填参数 → 模式构造器生成 DAG."""
        from app.core.llm import LLMClient
        from app.services.usage import CATEGORY_PLAN
        from app.agents.orchestration.patterns import build_pattern, pattern_catalog_text

        prompt = (
            "你是办公流程规划器。用户请求是带条件的半结构任务，请选择一个模式并抽取参数。\n"
            + pattern_catalog_text()
            + "\n只输出 JSON（不要 Markdown 围栏）：{\"pattern\": \"模式名\", \"params\": {\"参数名\": 值}}\n"
        )
        try:
            llm = LLMClient()
            reply = await llm.chat(
                [{"role": "user", "content": prompt + f"\n用户请求：{request}"}],
                temperature=0.1,
                max_tokens=2000,
                usage_user_id=user_id,
                usage_category=CATEGORY_PLAN,
                disable_reasoning_effort=True,
                api_key=llm_api_key,
            )
            data = _extract_json(reply)
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
            return None

    async def _call_planner(
        self,
        user_id: str,
        request: str,
        context: str,
        llm_api_key: str | None,
    ) -> str | None:
        """调用规划模型（用户配置的大模型），返回 JSON 计划文本（原样）."""
        prompt = _build_planner_prompt() + "\n" + context
        try:
            from app.core.llm import LLMClient
            from app.services.usage import CATEGORY_PLAN
            from app.agents.orchestration.cases import format_cases, get_similar_cases

            llm = LLMClient()
            # Few-Shot：相似历史成功任务作为规划参考
            similar = await get_similar_cases(request, 3)
            if similar:
                prompt += "\n\n" + format_cases(similar)
            reply = await llm.chat(
                [
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                # deepseek-v4-flash 等推理模型会先消耗大量 token 做 reasoning；
                # 预算太小会导致 content 为空、规划器回退规则版（单节点、不拆分文件）。
                # 给足 16000，保证规划 JSON 一定能产出。
                max_tokens=16000,
                usage_user_id=user_id,
                usage_category=CATEGORY_PLAN,
                reasoning_effort="low",
                api_key=llm_api_key,
            )
            return (reply or "").strip() or None
        except Exception:  # noqa: BLE001
            return None
        return None


def _extract_json(text: str) -> dict | None:
    """从模型输出中稳健提取 JSON（剥代码块围栏、取首个 { 到末尾 }）."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


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
