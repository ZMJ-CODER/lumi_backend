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

from app.agents.orchestration.models import TaskNode


class TaskTree:
    """规划结果：一组带依赖的任务节点."""

    def __init__(self, nodes: list[TaskNode], clarification: str | None = None):
        self.nodes = nodes
        self.clarification = clarification


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
    ) -> TaskTree:
        # 代码任务：显式指定 project_id，或在请求+澄清回答中匹配到已注册项目名
        combined = request + (f" {clarification_answer}" if clarification_answer else "")
        resolved_project = (
            project_id
            or (project_ids[0] if project_ids else None)
            or await self._match_project(user_id, combined)
        )
        if resolved_project:
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

PLANNER_JSON_PROMPT = (
    "你是任务规划器。把用户请求拆解为任务计划。\n"
    "可用执行 agent：\n"
    "- retrieval：检索知识库/项目索引定位信息，params 用 {\"query\": \"检索词\", \"top_k\": 5}\n"
    "- code_reader：在本地代码项目里定位并读取相关文件，params 用 {\"project_id\": \"项目ID\", \"instruction\": \"定位/分析指令\", \"target_file\": \"可选文件路径\"}\n"
    "- code_writer：生成或修改本地代码文件并写回，params 用 {\"project_id\": \"项目ID\", \"instruction\": \"编码指令\", \"target_file\": \"可选文件路径\", \"original_content\": \"可选，来自 reader\"}\n"
    "- code_tester：按项目类型自动选择并运行合适的验证命令（如 npm run build / pytest -q），params 用 {\"project_id\": \"项目ID\"}，不要预设 command，由 tester 根据项目文件自行决定\n"
    "- code_reviewer：审查已有代码或改动，params 用 {\"project_id\": \"项目ID\", \"instruction\": \"审查要求\", \"target_file\": \"可选文件路径\"}\n"
    "- code：旧版单节点代码任务（定位→生成→写回），params 用 {\"project_id\": \"项目ID\", \"instruction\": \"指令\"}\n"
    "代码任务建议按文件拆分节点，便于前端逐步展示进度：\n"
    "  - 每个需要阅读/定位的文件一个 code_reader 节点（按阅读顺序设置 depends_on）；\n"
    "  - 每个需要修改的文件一个 code_writer 节点，depends_on 指向对应的 reader 节点；\n"
    "  - 所有写入完成后一个 code_tester 节点；需要时最后加 code_reviewer。\n"
    "项目定位：根据用户请求与项目文件清单自动判断涉及哪个/哪些项目，不要因为未指定项目就澄清。\n"
    "涉及多个项目时按顺序生成任务：先完成一个项目再切换下一个（不同项目用各自的 project_id）。\n"
    "严格输出 JSON（不要代码块围栏、不要任何解释）：\n"
    "{\"tasks\":[{\"id\":\"t1\",\"name\":\"任务名\",\"agent\":\"retrieval\",\"params\":{},\"depends_on\":[]}],\"clarification\":\"\"}\n"
    "意图不明确或缺少关键信息（如未指定哪个项目）时，tasks 留空、clarification 填需要向用户确认的问题。"
)

# 执行层 agent 白名单（LlmPlanner 可调度的节点）
KNOWN_AGENTS = (
    "retrieval",
    "code",
    "code_reader",
    "code_writer",
    "code_tester",
    "code_reviewer",
)


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
    ) -> TaskTree:
        projects = await self._list_projects(user_id)
        tree = await self._plan_with_llm(
            user_id, request, project_id, project_ids, llm_api_key, projects, clarification_answer
        )
        if tree is not None:
            return tree
        return await self._fallback.plan(
            user_id, request, scene, project_id, project_ids, llm_api_key, clarification_answer
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
        )
        raw = await self._call_planner(user_id, context, llm_api_key)
        data = _extract_json(raw) if raw else None
        if not data:
            return None

        clarification = str(data.get("clarification") or "").strip()
        nodes: list[TaskNode] = []
        for t in data.get("tasks") or []:
            if not isinstance(t, dict):
                continue
            agent = t.get("agent")
            if agent not in KNOWN_AGENTS:
                continue
            params = dict(t.get("params") or {})
            if agent == "retrieval":
                params.setdefault("query", request)
            elif agent in KNOWN_AGENTS[1:]:
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
        return TaskTree(nodes=nodes, clarification=clarification)

    async def _call_planner(self, user_id: str, context: str, llm_api_key: str | None) -> str | None:
        """调用规划模型（用户配置的大模型），返回 JSON 计划文本（原样）."""
        prompt = PLANNER_JSON_PROMPT + "\n" + context
        try:
            from app.core.llm import LLMClient
            from app.services.usage import CATEGORY_PLAN

            llm = LLMClient()
            reply = await llm.chat(
                [
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=1024,
                usage_user_id=user_id,
                usage_category=CATEGORY_PLAN,
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
