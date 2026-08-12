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

from app.core.config import settings
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
        llm_api_key: str | None = None,
        clarification_answer: str | None = None,
    ) -> TaskTree:
        # 代码任务：显式指定 project_id，或在请求+澄清回答中匹配到已注册项目名
        combined = request + (f" {clarification_answer}" if clarification_answer else "")
        resolved_project = project_id or await self._match_project(user_id, combined)
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
    "- retrieval：检索知识库/项目索引定位信息，params 用 {\"query\": \"检索词\"}\n"
    "- code：在本地代码项目里修改/生成代码，params 用 {\"project_id\": \"项目ID\", \"instruction\": \"指令\"}\n"
    "严格输出 JSON（不要代码块围栏、不要任何解释）：\n"
    "{\"tasks\":[{\"id\":\"t1\",\"name\":\"任务名\",\"agent\":\"retrieval\",\"params\":{},\"depends_on\":[]}],\"clarification\":\"\"}\n"
    "意图不明确或缺少关键信息（如未指定哪个项目）时，tasks 留空、clarification 填需要向用户确认的问题。"
)


class LlmPlanner(Planner):
    """LLM 意图拆解：用户请求 → 任务树（DAG）.

    模型：配置了本地小模型（RAG_QUERY_REWRITE_PROVIDER=local）时走 Ollama 原生 API
    （think=False + function calling）；否则用用户当前选择的云端模型。
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
        llm_api_key: str | None = None,
        clarification_answer: str | None = None,
    ) -> TaskTree:
        projects = await self._list_projects(user_id)
        tree = await self._plan_with_llm(
            user_id, request, project_id, llm_api_key, projects, clarification_answer
        )
        if tree is not None:
            return tree
        return await self._fallback.plan(
            user_id, request, scene, project_id, llm_api_key, clarification_answer
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
        llm_api_key: str | None,
        projects: list[dict],
        clarification_answer: str | None = None,
    ) -> TaskTree | None:
        context = (
            f"用户请求：{request}\n"
            f"可用本地项目：{projects if projects else '无（retrieval 任务不需要项目）'}"
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
            if agent not in ("retrieval", "code"):
                continue
            params = dict(t.get("params") or {})
            if agent == "retrieval":
                params.setdefault("query", request)
            elif agent == "code":
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
        """调用规划模型（本地优先，其次云端用户模型），返回 JSON 计划文本（原样）."""
        prompt = PLANNER_JSON_PROMPT + "\n" + context
        if (
            settings.RAG_QUERY_REWRITE_PROVIDER == "local"
            and settings.RAG_QUERY_REWRITE_MODEL.strip()
        ):
            try:
                import httpx

                base = settings.RAG_QUERY_REWRITE_BASE_URL.rstrip("/")
                if base.endswith("/v1"):
                    base = base[: -len("/v1")]
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        f"{base}/api/chat",
                        json={
                            "model": settings.RAG_QUERY_REWRITE_MODEL.strip(),
                            "messages": [{"role": "user", "content": prompt}],
                            "stream": False,
                            "options": {"temperature": 0.1, "num_predict": 1024},
                        },
                    )
                    resp.raise_for_status()
                    return ((resp.json().get("message") or {}).get("content") or "").strip() or None
            except Exception:  # noqa: BLE001
                return None
            return None

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
