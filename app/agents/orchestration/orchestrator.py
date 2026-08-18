"""多智能体编排器 —— 提交任务 → 规划 → Temporal Workflow 执行 → 状态查询.

对外能力（API 契约不变）：
  - submit_job: 规划任务树 → 启动 Temporal Workflow（失败自动回退自建 DAG）
  - get_job / list_jobs: 查询状态（优先查询 Temporal，回退 Redis 快照）
  - cancel_job: 用户终止（signal 携带 keep_completed）
  - pause_job / resume_job: 暂停/恢复调度（signal）

状态由 Temporal 管理（Workflow query 返回 Job 快照）；Redis 仅保留
引导快照 + 用户任务索引（list_jobs 分页），以及 BYOK key 的短 TTL 临时桥接。
"""

import asyncio
import hashlib
import json
import time
import uuid

from loguru import logger

from app.agents.orchestration.dag import DagValidationError, execute_dag
from app.agents.orchestration.models import Job, JobStatus, TaskStatus
from app.agents.orchestration.planner import LlmPlanner, Planner
from app.agents.orchestration.review import ReviewHook, get_reviewer
from app.agents.orchestration.state import RedisStateStore, StateStore
from app.agents.orchestration.workers import WORKERS
from app.core.config import settings


class ActiveConversationJobError(RuntimeError):
    """同一会话已有尚未结束的办公任务。"""


class UserJobLimitError(RuntimeError):
    """单个用户同时运行的办公任务达到上限。"""


class AgentOrchestrator:
    """多智能体协作编排器（单例，全局复用）.

    temporal_enabled：None 时按 settings.AGENT_ORCHESTRATION 决定；
    测试/显式场景可传 False 强制走自建 DAG（legacy）。
    """

    def __init__(
        self,
        store: StateStore | None = None,
        planner: Planner | None = None,
        workers: dict | None = None,
        review: ReviewHook | None = None,
        temporal_enabled: bool | None = None,
    ):
        self._store = store or RedisStateStore()
        self._planner = planner or LlmPlanner()
        self._workers = workers if workers is not None else WORKERS
        self._review = review or get_reviewer()
        # ── Temporal 模式 ──
        self._temporal_mode = (
            settings.AGENT_ORCHESTRATION == "temporal"
            if temporal_enabled is None
            else bool(temporal_enabled)
        )
        self._temporal_available = False
        self._temporal_probe_at = 0.0
        self._temporal_unavailable_until = 0.0
        # ── legacy 自建 DAG 后台任务 ──
        self._tasks: dict[str, asyncio.Task] = {}  # job_id -> 后台执行任务
        # BYOK：legacy 路径的任务内临时 API key（仅内存，任务结束即释放）
        self._job_api_keys: dict[str, str] = {}
        # 同进程内串行化同一用户的“检查并提交”，避免两个并发请求同时越过限流检查。
        self._submission_locks: dict[str, asyncio.Lock] = {}

    # ── 办公短期记忆：跨任务记住"上一步做了什么"（摘要，非全文） ──

    _OFFICE_SUMMARY_KEY = "conv:office:sum:{conversation_id}"
    _OFFICE_SUMMARY_RECORDED = "conv:office:summed:{job_id}"
    _OFFICE_SUMMARY_MAX = 8

    async def _load_office_summaries(self, conversation_id: str) -> str:
        """读取该会话最近几次任务的摘要，拼接为给规划器的上下文（截断控制长度）."""
        if not conversation_id:
            return ""
        try:
            from app.core.redis import get_redis

            r = get_redis()
            items = await r.lrange(
                self._OFFICE_SUMMARY_KEY.format(conversation_id=conversation_id), 0, -1
            )
            if not items:
                return ""
            lines = []
            for idx, item in enumerate(reversed(items), 1):
                lines.append(f"{idx}. {str(item)[:300]}")
            return "\n".join(lines)[:3000]
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取办公任务摘要失败: {}", exc)
            return ""

    async def _record_office_summary(self, job: Job) -> None:
        """任务终结后把"请求+计划+结果"压成一条摘要，写入会话（幂等，每任务一次）."""
        if not job or not job.conversation_id:
            return
        if job.status not in (
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
        ):
            return
        try:
            from app.core.redis import get_redis

            r = get_redis()
            recorded_key = self._OFFICE_SUMMARY_RECORDED.format(job_id=job.job_id)
            if await r.exists(recorded_key):
                return
            result = job.result or {}
            final = str(result.get("final_answer") or result.get("answer") or "")
            summary = (
                f"任务：{job.request[:120]}"
                + (f" | 计划：{str(job.plan_text or '')[:150]}" if job.plan_text else "")
                + (f" | 结果：{final[:250]}" if final else "")
                + (f" | 失败：{str(job.error)[:100]}" if job.error else "")
            )
            key = self._OFFICE_SUMMARY_KEY.format(conversation_id=job.conversation_id)
            await r.rpush(key, summary[:600])
            await r.ltrim(key, -self._OFFICE_SUMMARY_MAX, -1)
            await r.setex(recorded_key, 86400 * 7, "1")
        except Exception as exc:  # noqa: BLE001
            logger.debug("写入办公任务摘要失败: {}", exc)

    async def _record_job_metric(self, job: Job) -> None:
        """任务结果指标（每个 job 只记一次终态）."""
        try:
            from app.core.observability import inc_agent_job
            from app.core.redis import get_redis

            key = f"obs:job:{job.job_id}"
            r = get_redis()
            if await r.set(key, "1", ex=86400 * 7, nx=True):
                inc_agent_job(str(job.status.value if hasattr(job.status, "value") else job.status))
        except Exception:  # noqa: BLE001
            pass

    # ── Temporal 可用性探测（成功缓存；失败 30s 后重试）──────────

    async def _probe_temporal(self) -> bool:
        if not self._temporal_mode:
            return False
        if self._temporal_available:
            return True
        now = time.monotonic()
        if now < self._temporal_unavailable_until:
            return False
        if now - self._temporal_probe_at < 30:
            return False
        self._temporal_probe_at = now
        try:
            from app.agents.orchestration.temporal.client import get_temporal_client

            await get_temporal_client()
            self._temporal_available = True
            self._temporal_unavailable_until = 0.0
            logger.info("Temporal 已连接: {}", settings.TEMPORAL_ADDRESS)
        except Exception as exc:  # noqa: BLE001
            # 开发环境/临时降级时，Temporal 不可用不应给每个办公任务增加连接超时。
            # 失败后较长时间负缓存，服务恢复后重启或管理员健康检查即可重新探测。
            self._temporal_unavailable_until = now + 300
            logger.warning("Temporal 不可用（{}），多智能体任务回退自建 DAG", exc)
        return self._temporal_available

    # ── 提交与执行 ───────────────────────────────────────────

    async def submit_job(
        self,
        user_id: str,
        request: str,
        scene: str = "office",
        conversation_id: str | None = None,
        project_id: str | None = None,
        project_ids: list[str] | None = None,
        llm_api_key: str | None = None,
        clarification_answer: str | None = None,
        office_docs: list[dict] | None = None,
        user_role: str = "user",
    ) -> Job:
        """规划任务树并启动执行（Temporal 优先），立即返回 Job."""
        submission_material = {
            "request": request,
            "scene": scene,
            "conversation_id": conversation_id,
            "project_id": project_id,
            "project_ids": sorted(str(x) for x in (project_ids or [])),
            "office_docs": sorted(
                str(d.get("doc_id")) for d in (office_docs or []) if d.get("doc_id")
            ),
            "clarification_answer": clarification_answer,
        }
        submission_key = hashlib.sha256(
            json.dumps(submission_material, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        # 幂等：30 秒内相同请求且任务未结束 → 直接返回（防双击/重复提交）
        try:
            for jid in await self._store.list_job_ids(user_id, 5):
                j = await self._store.get_job(jid)
                if (
                    j
                    and j.submission_key == submission_key
                    and time.time() - (j.created_at or 0) < 30
                    and j.status
                    in (JobStatus.RUNNING, JobStatus.PENDING, JobStatus.WAITING_APPROVAL)
                ):
                    logger.info("幂等命中：返回已提交的相同任务 {}", jid[:8])
                    return j
        except Exception:  # noqa: BLE001
            pass
        submission_lock = self._submission_locks.setdefault(user_id, asyncio.Lock())
        async with submission_lock:
            # 锁内再次检查幂等，覆盖两个并发双击请求同时通过锁外检查的竞态。
            try:
                for jid in await self._store.list_job_ids(user_id, 5):
                    existing = await self._store.get_job(jid)
                    if (
                        existing
                        and existing.submission_key == submission_key
                        and time.time() - (existing.created_at or 0) < 30
                        and existing.status
                        in (JobStatus.RUNNING, JobStatus.PENDING, JobStatus.WAITING_APPROVAL)
                    ):
                        return existing
            except Exception:  # noqa: BLE001
                pass
            active_statuses = {
                JobStatus.PENDING,
                JobStatus.RUNNING,
                JobStatus.PAUSED,
                JobStatus.WAITING_APPROVAL,
            }
            active_jobs = [
                job
                for job in await self.list_jobs(user_id, 50)
                if job.status in active_statuses
            ]
            if conversation_id and any(
                job.conversation_id == conversation_id for job in active_jobs
            ):
                raise ActiveConversationJobError(
                    "当前会话已有办公任务正在执行，请等待完成或主动终止任务。"
                )
            if len(active_jobs) >= 2:
                raise UserJobLimitError("当前有任务正在进行中，请切换到普通模式对话")

            return await self._submit_job_unlocked(
                user_id=user_id,
                request=request,
                scene=scene,
                conversation_id=conversation_id,
                project_id=project_id,
                project_ids=project_ids,
                llm_api_key=llm_api_key,
                clarification_answer=clarification_answer,
                office_docs=office_docs,
                user_role=user_role,
                submission_key=submission_key,
            )

    async def _submit_job_unlocked(
        self,
        *,
        user_id: str,
        request: str,
        scene: str,
        conversation_id: str | None,
        project_id: str | None,
        project_ids: list[str] | None,
        llm_api_key: str | None,
        clarification_answer: str | None,
        office_docs: list[dict] | None,
        user_role: str,
        submission_key: str,
    ) -> Job:
        """已持有用户提交锁时完成规划和任务创建。"""
        # 办公短期记忆：加载本会话此前任务的摘要，注入规划上下文
        prior_summaries = (
            await self._load_office_summaries(conversation_id) if conversation_id else ""
        )
        tree = await self._planner.plan(
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
        self._prefer_atomic_steps(tree.nodes, request)
        self._serialize_steps(tree.nodes)
        job = Job(
            job_id=str(uuid.uuid4()),
            user_id=user_id,
            user_role=user_role,
            request=request,
            scene=scene,
            conversation_id=conversation_id,
            submission_key=submission_key,
            status=JobStatus.RUNNING,
            nodes=tree.nodes,
            plan_text=tree.plan_text,
        )
        from app.agents.orchestration.safety import prepare_node_safety

        for node in job.nodes:
            prepare_node_safety(node, user_id, job.job_id)
        # DAG 静态校验：agent 已注册 / 必选参数 / 无环 / id 唯一；
        # 校验失败 → 降级为知识库检索（简化流程），避免"LLM 瞎指挥"产生必失败的 DAG
        from app.agents.orchestration.dag import validate_planned_dag
        from app.agents.orchestration.models import TaskNode

        dag_errors = validate_planned_dag(job.nodes, self._workers)
        if dag_errors:
            logger.warning(
                "任务 DAG 校验失败，降级为检索流程: {} | {}", job.job_id[:8], "；".join(dag_errors)[:300]
            )
            job.nodes = [
                TaskNode(
                    id=f"r{int(time.time())}-{uuid.uuid4().hex[:6]}",
                    name="知识库检索（规划降级）",
                    agent="atomic_step",
                    params={
                        "instruction": request,
                        "preferred_tool": "query_knowledge",
                        "inputs": {"query": request, "top_k": 5},
                    },
                    depends_on=[],
                )
            ]
        # 意图不明确：LLM 请求向用户澄清，任务以"待澄清"结果直接收敛（不启动执行）
        if tree.clarification and not tree.nodes:
            job.status = JobStatus.COMPLETED
            job.result = {"type": "clarification", "question": tree.clarification}
            await self._store.create_job(job)
            logger.info("任务需澄清（不启动执行）: {} | {}", job.job_id[:8], tree.clarification[:80])
            return job

        if await self._probe_temporal():
            try:
                await self._submit_temporal(job, llm_api_key)
                logger.info(
                    "多智能体任务已提交(Temporal): {} | agent={} request={}",
                    job.job_id[:8],
                    [n.agent for n in job.nodes],
                    request[:40],
                )
                return job
            except Exception as exc:  # noqa: BLE001
                logger.warning("Temporal 提交任务失败，回退自建 DAG: {} | {}", job.job_id[:8], exc)

        # ── legacy 自建 DAG 回退 ──
        await self._store.create_job(job)
        if llm_api_key:
            self._job_api_keys[job.job_id] = llm_api_key
        task = asyncio.create_task(self._run_job(job.job_id))
        self._tasks[job.job_id] = task
        logger.info(
            "多智能体任务已提交(legacy): {} | agent={} request={}",
            job.job_id[:8],
            [n.agent for n in job.nodes],
            request[:40],
        )
        return job

    @staticmethod
    def _prefer_atomic_steps(nodes, request: str) -> None:
        """把常见办公角色节点迁移为无角色能力锁的通用原子步骤.

        旧 agent/API 继续兼容；代码编写、脚本生成等复合实现暂保留专业执行器。
        """
        tool_map = {
            "retrieval": "query_knowledge",
            "web_research": "web_search",
            "office_todo": "todo_manager",
            "office_calendar": "calendar_manager",
        }
        text_tools = {
            "email": "compose_email",
            "doc": "compose_official_doc",
            "rewrite": "rewrite_text",
            "summary": "summarize_text",
            "minutes": "meeting_minutes",
            "extract": "extract_info",
            "invoice": "invoice_parse",
            "compliance": "compliance_check",
        }
        research_tools = {
            "competitor": "competitor_analysis",
            "document_qa": "document_qa",
            "customer_service": "customer_service",
            "daily_report": "daily_report",
        }
        doc_tools = {
            "read": "office_doc_read",
            "edit": "office_doc_edit",
            "analyze": "office_doc_analyze",
        }
        system_tools = {
            "open_app": "open_app",
            "open_file": "open_file",
            "open_url": "open_url",
            "send_email": "send_email",
            "ps": "ps",
            "kill": "kill",
            "env": "env",
            "datetime": "get_datetime",
            "curl": "curl",
        }
        for node in nodes:
            preferred = tool_map.get(node.agent)
            if node.agent == "office_text":
                preferred = text_tools.get(str(node.params.get("task") or ""))
            elif node.agent == "office_research":
                preferred = research_tools.get(str(node.params.get("mode") or ""))
            elif node.agent == "office_doc":
                preferred = doc_tools.get(str(node.params.get("mode") or "read"))
            elif node.agent == "office_system":
                preferred = system_tools.get(str(node.params.get("task") or "open_app"))
            if not preferred:
                continue
            old_agent = node.agent
            original = dict(node.params or {})
            instruction = str(
                original.get("instruction")
                or original.get("query")
                or original.get("content")
                or node.name
                or request
            )
            node.agent = "atomic_step"
            node.params = {
                "instruction": instruction,
                "preferred_tool": preferred,
                "fallback_tools": (
                    ["python_exec"]
                    if preferred in {"office_doc_read", "office_doc_analyze"}
                    else []
                ),
                "inputs": original,
            }
            node.metadata = {**(node.metadata or {}), "legacy_agent": old_agent}

    @staticmethod
    def _serialize_steps(nodes) -> None:
        """按拓扑顺序把办公计划收敛为单链，确保任意时刻只执行一个原子步骤。"""
        if len(nodes) < 2:
            return
        by_id = {node.id: node for node in nodes}
        indegree = {node.id: 0 for node in nodes}
        children = {node.id: [] for node in nodes}
        for node in nodes:
            for dep in node.depends_on:
                if dep in by_id:
                    indegree[node.id] += 1
                    children[dep].append(node.id)
        ready = [node.id for node in nodes if indegree[node.id] == 0]
        ordered = []
        while ready:
            node_id = ready.pop(0)
            ordered.append(by_id[node_id])
            for child_id in children[node_id]:
                indegree[child_id] -= 1
                if indegree[child_id] == 0:
                    ready.append(child_id)
        if len(ordered) != len(nodes):
            return
        for index, node in enumerate(ordered):
            node.depends_on = [] if index == 0 else [ordered[index - 1].id]
        nodes[:] = ordered

    async def _submit_temporal(self, job: Job, llm_api_key: str | None) -> None:
        """启动 Temporal Workflow：payload = Job dict + 节点执行配置."""
        from app.agents.orchestration.temporal.client import (
            start_agent_workflow,
            store_byok_key,
        )

        payload = job.model_dump()
        payload["config"] = {
            "node_timeout_seconds": settings.AGENT_NODE_TIMEOUT_SECONDS,
            "node_max_retries": settings.AGENT_NODE_MAX_RETRIES,
            "node_concurrency": 1,
        }
        if llm_api_key:
            await store_byok_key(job.job_id, llm_api_key)
        await start_agent_workflow(payload, job.job_id)
        # 引导快照 + 用户任务索引（list_jobs 分页；workflow 运行后以 Temporal 状态为准）
        await self._store.create_job(job)

    async def _run_job(self, job_id: str) -> None:
        """legacy 后台执行：校验 DAG → 拓扑执行 → 汇总状态."""
        llm_api_key = self._job_api_keys.pop(job_id, None)
        try:
            job = await self._store.get_job(job_id)
            if job is None:
                return
            await execute_dag(
                job,
                self._workers,
                self._review,
                self._store,
                concurrency=1,
                llm_api_key=llm_api_key,
            )
            job = await self._store.get_job(job_id)
            if job and job.status == JobStatus.COMPLETED and not job.result:
                results = []
                for node in job.nodes:
                    value = node.result or {}
                    content = value.get("content") or value.get("output") or ""
                    if content:
                        results.append(
                            {
                                "agent": node.agent,
                                "title": node.name or node.agent,
                                "content": str(content)[:30000],
                            }
                        )
                # 只有两个及以上步骤或明确没有最终回答时才需要额外汇总模型调用。
                # 单一步骤（如脚本转换）直接采用步骤产出，省一次完整上下文的模型请求。
                if len(results) == 1:
                    job.result = {"final_answer": results[0]["content"]}
                    await self._store.save_job(job)
                elif results:
                    try:
                        from app.agents.orchestration.temporal.activities import (
                            synthesize_final_answer_activity,
                        )

                        synthesized = await synthesize_final_answer_activity(
                            {
                                "user_id": job.user_id,
                                "job_id": job.job_id,
                                "request": job.request,
                                "nodes": results,
                            }
                        )
                        if synthesized.get("final_answer"):
                            job.result = synthesized
                            await self._store.save_job(job)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("legacy DAG 最终答案汇总失败 {}: {}", job_id, exc)
        except DagValidationError as exc:
            logger.error("任务 DAG 非法 {}: {}", job_id, exc)
            job = await self._store.get_job(job_id)
            if job:
                job.status = JobStatus.FAILED
                job.error = str(exc)
                job.updated_at = time.time()
                await self._store.save_job(job)
        except asyncio.CancelledError:
            logger.info("任务后台执行被取消: {}", job_id)
            job = await self._store.get_job(job_id)
            if job and job.status not in (JobStatus.CANCELLED, JobStatus.INTERRUPTED):
                job.status = JobStatus.INTERRUPTED
                job.updated_at = time.time()
                await self._store.save_job(job)
        except Exception as exc:  # noqa: BLE001
            logger.error("任务执行异常 {}: {}", job_id, exc)
            job = await self._store.get_job(job_id)
            if job:
                job.status = JobStatus.FAILED
                job.error = str(exc)
                job.updated_at = time.time()
                await self._store.save_job(job)
        finally:
            self._tasks.pop(job_id, None)
            self._job_api_keys.pop(job_id, None)

    # ── 查询 ────────────────────────────────────────────────

    async def get_job(self, job_id: str) -> Job | None:
        """优先查询 Temporal 工作流状态，失败回退 Redis 快照."""
        job = None
        if await self._probe_temporal():
            try:
                from app.agents.orchestration.temporal.client import query_agent_job

                snap = await query_agent_job(job_id)
                if snap is not None:
                    job = Job.model_validate(snap)
            except Exception as exc:  # noqa: BLE001
                logger.warning("查询 Temporal 任务状态失败，回退快照 {}: {}", job_id, exc)
        if job is None:
            job = await self._store.get_job(job_id)
        if job is None:
            return None
        # 办公短期记忆：任务终结时落一条"上一步做了什么"摘要（幂等）
        await self._record_office_summary(job)
        await self._record_job_metric(job)
        return await self._attach_progress(job)

    async def _attach_progress(self, job: Job) -> Job:
        """把 Redis 中的节点实时进度合并进 node.metadata["progress"]（仅展示用）."""
        try:
            from app.agents.core.progress import get_job_progress

            progress = await get_job_progress(job.job_id)
            if progress:
                for n in job.nodes:
                    text = progress.get(n.id)
                    if text:
                        n.metadata = {**(n.metadata or {}), "progress": str(text)}
        except Exception as exc:  # noqa: BLE001
            logger.debug("合并任务进度失败 {}: {}", job.job_id, exc)
        return job

    async def list_jobs(self, user_id: str, limit: int = 20) -> list[Job]:
        """按用户索引倒序列出任务（Temporal/legacy 混合列表均支持）."""
        ids = await self._store.list_job_ids(user_id, limit)
        jobs: list[Job] = []
        for jid in ids:
            job = await self.get_job(jid)
            if job:
                jobs.append(job)
        return jobs

    async def admin_list_jobs(self, limit: int = 50) -> list[Job]:
        """管理后台：跨用户列出最近任务（全量索引 + Temporal 查询）. """
        ids = await self._store.list_all_job_ids(limit)
        jobs: list[Job] = []
        for jid in ids:
            job = await self.get_job(jid)
            if job:
                jobs.append(job)
        return jobs

    # ── 控制：终止 / 暂停 / 恢复 ─────────────────────────────

    async def cancel_job(self, job_id: str, keep_completed: bool = True) -> Job | None:
        """终止任务：Temporal 路径发 cancel_request signal（可保留已完成节点）."""
        if await self._probe_temporal():
            try:
                from app.agents.orchestration.temporal.client import (
                    cancel_agent_workflow,
                    query_agent_job,
                )

                snap = await query_agent_job(job_id)
                if snap is not None:
                    job = Job.model_validate(snap)
                    if job.status in (
                        JobStatus.COMPLETED,
                        JobStatus.FAILED,
                        JobStatus.CANCELLED,
                    ):
                        return job
                    await cancel_agent_workflow(job_id, keep_completed)
                    job.status = JobStatus.CANCELLED
                    job.updated_at = time.time()
                    return job
            except Exception as exc:  # noqa: BLE001
                logger.warning("Temporal 取消任务失败，回退快照 {}: {}", job_id, exc)

        # legacy：状态驱动，execute_dag 轮询到 CANCELLED 后自行收尾
        job = await self._store.get_job(job_id)
        if job is None or job.status in (JobStatus.COMPLETED, JobStatus.FAILED):
            return job
        job.status = JobStatus.CANCELLED
        job.updated_at = time.time()
        if not keep_completed:
            for n in job.nodes:
                if n.status in (
                    TaskStatus.PENDING,
                    TaskStatus.READY,
                    TaskStatus.RUNNING,
                    TaskStatus.RETRYING,
                ):
                    n.status = TaskStatus.CANCELLED
                    n.error = "任务被用户终止"
        await self._store.save_job(job)
        return job

    async def approve_job(self, job_id: str, node_id: str, approved: bool = True) -> None:
        """人工审批：向工作流发送 approve_task 信号（高风险节点门控）."""
        if not await self._probe_temporal():
            raise RuntimeError("Temporal 不可用，无法审批")
        from app.agents.orchestration.temporal.client import approve_agent_workflow

        await approve_agent_workflow(job_id, node_id, approved)

    async def pause_job(self, job_id: str) -> Job | None:
        """暂停任务（不调度新节点；运行中的节点会执行完）."""
        if await self._probe_temporal():
            try:
                from app.agents.orchestration.temporal.client import (
                    pause_agent_workflow,
                    query_agent_job,
                )

                snap = await query_agent_job(job_id)
                if snap is not None:
                    job = Job.model_validate(snap)
                    if job.status == JobStatus.RUNNING:
                        await pause_agent_workflow(job_id)
                        job.status = JobStatus.PAUSED
                        job.updated_at = time.time()
                    return job
            except Exception as exc:  # noqa: BLE001
                logger.warning("Temporal 暂停任务失败，回退快照 {}: {}", job_id, exc)

        job = await self._store.get_job(job_id)
        if job is None or job.status != JobStatus.RUNNING:
            return job
        job.status = JobStatus.PAUSED
        job.updated_at = time.time()
        await self._store.save_job(job)
        return job

    async def resume_job(self, job_id: str) -> Job | None:
        """恢复被暂停的任务."""
        if await self._probe_temporal():
            try:
                from app.agents.orchestration.temporal.client import (
                    query_agent_job,
                    resume_agent_workflow,
                )

                snap = await query_agent_job(job_id)
                if snap is not None:
                    job = Job.model_validate(snap)
                    if job.status == JobStatus.PAUSED:
                        await resume_agent_workflow(job_id)
                        job.status = JobStatus.RUNNING
                        job.updated_at = time.time()
                    return job
            except Exception as exc:  # noqa: BLE001
                logger.warning("Temporal 恢复任务失败，回退快照 {}: {}", job_id, exc)

        job = await self._store.get_job(job_id)
        if job is None or job.status != JobStatus.PAUSED:
            return job
        job.status = JobStatus.RUNNING
        job.updated_at = time.time()
        await self._store.save_job(job)
        return job


# 全局单例（API 层使用）
orchestrator = AgentOrchestrator()
