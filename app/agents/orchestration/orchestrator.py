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
from app.agents.orchestration.plan_cache import PlanCache, build_plan_cache_key
from app.agents.orchestration.review import ReviewHook, get_reviewer
from app.agents.orchestration.tca import ComplexityLevel, TaskComplexityAssessor
from app.agents.orchestration.task_manifest import (
    apply_manifest_batch_results,
    authorize_manifest_source,
    extract_natural_language_manifest,
    has_unsafe_manifest_instruction,
    manifest_final_answer,
    manifest_progress,
    materialize_manifest_batch,
    new_manifest,
    parse_task_manifest,
)
from app.agents.orchestration.state import RedisStateStore, StateStore
from app.agents.orchestration.workers import WORKERS
from app.agents.orchestration.admission import (
    AdmissionBackpressureError,
    job_admission,
)
from app.core.config import settings


class ActiveConversationJobError(RuntimeError):
    """同一会话已有尚未结束的办公任务。"""


class UserJobLimitError(RuntimeError):
    """单个用户同时运行的办公任务达到上限。"""


class AgentBackpressureError(RuntimeError):
    """全局办公容量或规划准入槽位已满。"""


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
        complexity_assessor: TaskComplexityAssessor | None = None,
        plan_cache: PlanCache | None = None,
    ):
        self._store = store or RedisStateStore()
        self._planner = planner or LlmPlanner()
        self._workers = workers if workers is not None else WORKERS
        self._review = review or get_reviewer()
        self._complexity_assessor = complexity_assessor or TaskComplexityAssessor()
        self._plan_cache = plan_cache or PlanCache()
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
        # legacy DAG 在 API 进程内执行。Redis 短暂不可读时保留运行中快照，
        # 防止前端已收到 job_id 却立即被 404；Redis 恢复后仍优先使用其快照。
        self._live_jobs: dict[str, Job] = {}
        # 同进程内串行化同一用户的“检查并提交”，避免两个并发请求同时越过限流检查。
        self._submission_locks: dict[str, asyncio.Lock] = {}
        # 计划缓存只在任务成功后提交。上下文不进入 Job/API，避免把内部文档 ID
        # 或项目绑定暴露给前端；进程异常时最多损失一次缓存学习，不影响执行正确性。
        self._job_plan_context: dict[str, dict] = {}
        self._pending_plan_cache: dict[str, tuple[str, list[dict] | None]] = {}

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
            if len(active_jobs) >= settings.AGENT_USER_ACTIVE_JOB_LIMIT:
                raise UserJobLimitError("当前有任务正在进行中，请切换到普通模式对话")
            # 规划同样会占用模型与 CPU；先占一个短期准入槽，避免高峰时大量请求
            # 同时进入 planner。成功创建可执行 Job 后该槽会提升为活跃任务槽。
            admission_token = str(uuid.uuid4())
            try:
                await job_admission.reserve(admission_token)
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
                    admission_token=admission_token,
                )
            except AdmissionBackpressureError as exc:
                await job_admission.release(token=admission_token)
                raise AgentBackpressureError(str(exc)) from exc
            except Exception:
                await job_admission.release(token=admission_token)
                raise

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
        admission_token: str,
    ) -> Job:
        """已持有用户提交锁时完成规划和任务创建。"""
        # 办公短期记忆：加载本会话此前任务的摘要，注入规划上下文
        prior_summaries = (
            await self._load_office_summaries(conversation_id) if conversation_id else ""
        )
        routing: dict = {}
        cache_key = ""
        cache_hit = False
        level = ComplexityLevel.M2
        manifest_items: list[str] | list[dict] = []
        manifest_source: dict = {}
        manifest_clarification = ""
        if scene == "office":
            authorization = authorize_manifest_source(request, office_docs)
            if authorization and authorization.clarification:
                manifest_clarification = authorization.clarification
            elif authorization:
                source_text = request
                source_label = "用户消息"
                if authorization.source == "office_document":
                    # ``office_docs`` is client-provided routing metadata only.
                    # Re-resolve the selected session on the server so an
                    # attachment can never authorize access to another user’s
                    # document just by forging a doc_id or filename.
                    selected = authorization.document or {}
                    selected_doc_id = str(selected.get("doc_id") or "")
                    try:
                        from app.core.executors import run_in_compute
                        from app.services.office_docs import ensure_session, extract_full_text

                        meta = await ensure_session(user_id, selected_doc_id)
                        expected_name = str(selected.get("filename") or "")
                        actual_name = str(meta.get("filename") or "")
                        if expected_name and actual_name and (
                            expected_name.casefold() != actual_name.casefold()
                        ):
                            raise ValueError("附件名称与服务端会话记录不一致")
                        source_text = await run_in_compute(extract_full_text, user_id, selected_doc_id)
                        source_label = f"用户指定附件《{actual_name}》"
                        manifest_source = {
                            "type": "office_document",
                            "doc_id": selected_doc_id,
                            "filename": actual_name,
                        }
                    except (LookupError, ValueError) as exc:
                        manifest_clarification = f"无法读取指定的清单附件：{exc}。请重新上传或确认文件名后再试。"
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("读取已授权清单附件失败: {}", exc)
                        manifest_clarification = "指定的清单附件暂时无法读取，请稍后重试或将清单粘贴到消息中。"
                else:
                    manifest_source = {"type": "user_message"}

                if not manifest_clarification:
                    # Fail closed before either the deterministic parser or a
                    # model sees this source as executable work.  A document
                    # can contain arbitrary quoted text, but an explicitly
                    # selected *execution checklist* containing an override /
                    # data-exfiltration instruction must never partly run.
                    if has_unsafe_manifest_instruction([source_text]):
                        manifest_clarification = (
                            "清单中包含试图改变系统规则、访问敏感数据或越权资源的内容，"
                            "因此未启动执行。请移除该内容后重新提交合法任务。"
                        )
                    else:
                        # Explicitly numbered entries are deterministic and do
                        # not spend an LLM call. Natural-language checklists
                        # are cleaned only after this control-plane
                        # authorization.
                        manifest_items = parse_task_manifest(source_text)
                        if not manifest_items:
                            try:
                                manifest_items = await extract_natural_language_manifest(
                                    source_text,
                                    user_id=user_id,
                                    api_key=llm_api_key,
                                    source_label=source_label,
                                )
                            except Exception as exc:  # noqa: BLE001
                                logger.info("清单清洗模型不可用，要求用户提供显式编号清单: {}", exc)
                                manifest_clarification = (
                                    "无法可靠识别该自然语言清单。请将任务改为编号或项目符号列表后重试，"
                                    "我会严格按顺序执行。"
                                )
                        normalized_instructions = [
                            str(item.get("instruction") or "") if isinstance(item, dict) else str(item)
                            for item in manifest_items
                        ]
                        if manifest_items and has_unsafe_manifest_instruction(normalized_instructions):
                            manifest_items = []
                            manifest_clarification = (
                                "清单中包含试图改变系统规则、访问敏感数据或越权资源的内容，"
                                "因此未启动执行。请移除该内容后重新提交合法任务。"
                            )
                        elif manifest_items and len(manifest_items) < 8:
                            # Short lists remain ordinary office tasks; only
                            # long lists use the rolling-manifest executor.
                            manifest_items = []
        if manifest_clarification:
            from app.agents.orchestration.planner import TaskTree

            tree = TaskTree(nodes=[], clarification=manifest_clarification)
            routing = {
                "level": "manifest",
                "mode": "manifest_clarification",
                "cache_hit": False,
                "plan_revision": 1,
                "manifest_source": manifest_source,
            }
        elif manifest_items:
            # Long, explicitly enumerated work is controlled by a persisted
            # manifest. It deliberately bypasses the one-shot Planner JSON.
            from app.agents.orchestration.planner import TaskTree

            manifest = new_manifest(manifest_items, source=manifest_source)
            tree = TaskTree(
                nodes=materialize_manifest_batch(manifest),
                plan_text=f"已识别 {len(manifest_items)} 项清单，按每批 {manifest['batch_size']} 项依次执行。",
            )
            routing = {
                "level": "manifest",
                "mode": "rolling_manifest",
                "cache_hit": False,
                "plan_revision": 1,
                "manifest": manifest,
                "manifest_progress": manifest_progress(manifest),
            }
        elif scene == "office":
            route_started = time.perf_counter()
            assessment = await self._complexity_assessor.assess(
                request,
                office_docs=office_docs,
                prior_summaries=prior_summaries,
            )
            level = assessment.level
            routing = {
                **assessment.audit_dict(),
                "cache_hit": False,
                "replan_count": 0,
                "upgrade_count": 0,
                "upgrades": [],
                "plan_revision": 1,
                "plan_history": [],
            }
            capability_parts = [f"worker:{name}" for name in sorted(self._workers)]
            try:
                from app.agents.skills.registry import SkillRegistry

                capability_parts.extend(
                    f"skill:{skill.name}:{skill.category}:{skill.environment}:"
                    f"{int(skill.write_op)}:{int(skill.idempotent)}"
                    for skill in sorted(SkillRegistry.list(), key=lambda item: item.name)
                )
            except Exception:  # noqa: BLE001
                pass
            capability_signature = hashlib.sha256(
                "|".join(capability_parts).encode("utf-8")
            ).hexdigest()[:16]
            cache_allowed = (
                level in {ComplexityLevel.M1, ComplexityLevel.M2}
                and not project_id
                and not project_ids
                and not clarification_answer
                and not prior_summaries
            )
            if cache_allowed:
                cache_key = build_plan_cache_key(
                    user_id=user_id,
                    request=request,
                    scene=scene,
                    user_role=user_role,
                    office_docs=office_docs,
                    capability_signature=capability_signature,
                )
                cached = await self._plan_cache.get(cache_key, office_docs)
                try:
                    from app.core.observability import inc_plan_cache

                    inc_plan_cache("hit" if cached else "miss")
                except Exception:  # noqa: BLE001
                    pass
                if cached:
                    from app.agents.orchestration.planner import TaskTree

                    cached_nodes, cached_plan_text = cached
                    tree = TaskTree(nodes=cached_nodes, plan_text=cached_plan_text)
                    cache_hit = True
                else:
                    tree = None
            else:
                tree = None
            if tree is None:
                from app.agents.orchestration.routing import plan_for_level

                tree = await plan_for_level(
                    self._planner,
                    level,
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
            routing["cache_hit"] = cache_hit
            route_duration = time.perf_counter() - route_started
            routing["route_latency_ms"] = int(route_duration * 1000)
            try:
                from app.core.observability import inc_agent_route

                inc_agent_route(level.value, assessment.mode.value, cache_hit, route_duration)
            except Exception:  # noqa: BLE001
                pass
        else:
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
        from app.agents.orchestration.presentation import attach_display_plan

        plan_revision = int(routing.get("plan_revision") or 1) if scene == "office" else 1
        for node in tree.nodes:
            node.metadata = {**(node.metadata or {}), "plan_revision": plan_revision}
            attach_display_plan(node)
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
            routing=routing,
        )
        if scene == "office":
            self._job_plan_context[job.job_id] = {
                "user_id": user_id,
                "request": request,
                "scene": scene,
                "project_id": project_id,
                "project_ids": project_ids,
                "llm_api_key": llm_api_key,
                "clarification_answer": clarification_answer,
                "office_docs": office_docs,
                "prior_summaries": prior_summaries,
            }
            if cache_key and not cache_hit:
                self._pending_plan_cache[job.job_id] = (cache_key, office_docs)
        # 余额、密钥、模型下架、配置不兼容等规划错误是明确终态；不能伪装成
        # “知识库检索”或启动一个没有可执行步骤的任务。
        if tree.error:
            job.status = JobStatus.FAILED
            job.error = tree.error
            job.result = {
                "type": "planning_error",
                "error_code": tree.error_code or "PLANNING_ERROR",
                "message": tree.error,
            }
            await self._store.create_job(job)
            await job_admission.release(token=admission_token)
            self._discard_pending_learning(job.job_id)
            logger.warning("办公任务规划已停止: {} | {}", job.job_id[:8], tree.error)
            return job
        from app.agents.orchestration.safety import prepare_node_safety

        for node in job.nodes:
            prepare_node_safety(node, user_id, job.job_id)
        # DAG 静态校验：agent 已注册 / 必选参数 / 无环 / id 唯一。
        # 无效规划必须如实终止，不能改造成无关的知识库检索。
        from app.agents.orchestration.dag import validate_planned_dag

        dag_errors = validate_planned_dag(job.nodes, self._workers)
        if dag_errors:
            detail = "；".join(dag_errors)[:500]
            logger.warning(
                "任务 DAG 校验失败，终止任务: {} | {}", job.job_id[:8], detail
            )
            job.status = JobStatus.FAILED
            job.error = "任务规划校验失败，未执行任何工具。请稍后重试；若持续出现，请切换模型或检查任务描述。"
            job.result = {
                "type": "planning_error",
                "error_code": "DAG_VALIDATION_ERROR",
                "detail": detail,
                "message": job.error,
            }
            await self._store.create_job(job)
            await job_admission.release(token=admission_token)
            self._discard_pending_learning(job.job_id)
            return job
        # 意图不明确：LLM 请求向用户澄清，任务以"待澄清"结果直接收敛（不启动执行）
        if tree.clarification and not tree.nodes:
            job.status = JobStatus.COMPLETED
            job.result = {"type": "clarification", "question": tree.clarification}
            await self._store.create_job(job)
            await job_admission.release(token=admission_token)
            self._discard_pending_learning(job.job_id)
            logger.info("任务需澄清（不启动执行）: {} | {}", job.job_id[:8], tree.clarification[:80])
            return job

        # 任务已经通过规划和 DAG 校验，现在将短期提交槽提升成长期活跃槽。
        # 任何后续提交异常都会被外层 except 清理；终态由 _run_job/get_job 清理。
        await job_admission.promote(admission_token, job.job_id, user_id)
        try:
            # Temporal workflow input is intentionally static. A manifest grows
            # batches after execution, so run it through the stateful legacy
            # executor until a Temporal continue-as-new workflow is added.
            if not job.routing.get("manifest") and await self._probe_temporal():
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
            self._live_jobs[job.job_id] = job
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
        except Exception:
            # 提升成功但创建任务/投递 worker 失败时没有终态回调，必须立即归还容量。
            await job_admission.release(job_id=job.job_id, user_id=user_id)
            raise

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
                    ["office_doc_read"]
                    if preferred == "office_doc_analyze"
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
            job = await self._store.get_job(job_id) or self._live_jobs.get(job_id)
            if job is None:
                return
            while True:
                await execute_dag(
                    job,
                    self._workers,
                    self._review,
                    self._store,
                    concurrency=1,
                    llm_api_key=llm_api_key,
                )
                job = await self._store.get_job(job_id) or job
                self._live_jobs[job_id] = job
                if await self._continue_manifest_job(job):
                    job = await self._store.get_job(job_id) or job
                    self._live_jobs[job_id] = job
                    continue
                if not await self._maybe_replan_failed_job(job, llm_api_key):
                    break
                job = await self._store.get_job(job_id) or job
                self._live_jobs[job_id] = job
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
            self._live_jobs.pop(job_id, None)
            # legacy 执行器的 finally 是最可靠的容量释放点（包括取消/异常）。
            try:
                finished = await self._store.get_job(job_id)
                if finished and finished.status in {
                    JobStatus.COMPLETED,
                    JobStatus.FAILED,
                    JobStatus.CANCELLED,
                    JobStatus.INTERRUPTED,
                }:
                    await job_admission.release(job_id=job_id, user_id=finished.user_id)
                if finished:
                    await self._learn_from_finished_job(finished)
            except Exception as exc:  # noqa: BLE001
                logger.debug("释放办公任务准入槽失败 {}: {}", job_id, exc)
            self._job_plan_context.pop(job_id, None)

    async def _continue_manifest_job(self, job: Job) -> bool:
        """Commit one long-list batch and materialize the next one when due.

        Returns ``True`` only when the caller should immediately execute another
        batch.  Nodes are replaced, rather than appended, because ``execute_dag``
        treats its supplied graph as a fresh execution window.  The manifest is
        the durable history and contains the per-item terminal state.
        """
        manifest = (job.routing or {}).get("manifest")
        if not isinstance(manifest, dict):
            return False
        if job.status in {JobStatus.CANCELLED, JobStatus.INTERRUPTED, JobStatus.PAUSED}:
            return False
        apply_manifest_batch_results(manifest, job.nodes)
        progress = manifest_progress(manifest)
        job.routing = dict(job.routing or {})
        job.routing["manifest"] = manifest
        job.routing["manifest_progress"] = progress
        if progress["cursor"] >= progress["total"]:
            # Some items may fail, but the list itself was fully processed.
            # Preserve failures in the manifest and make the aggregate terminal
            # status completed so the user receives its final audit instead of
            # an apparently stuck job.
            job.status = JobStatus.COMPLETED
            job.error = None
            job.result = {
                "type": "task_manifest",
                "final_answer": manifest_final_answer(manifest),
                "manifest_progress": progress,
            }
            job.updated_at = time.time()
            await self._store.save_job(job)
            return False

        from app.agents.orchestration.presentation import attach_display_plan
        from app.agents.orchestration.safety import prepare_node_safety

        revision = int(job.routing.get("plan_revision") or 1) + 1
        next_nodes = materialize_manifest_batch(manifest, revision=revision)
        if not next_nodes:
            job.status = JobStatus.FAILED
            job.error = "任务清单没有可执行的后续步骤"
            job.updated_at = time.time()
            await self._store.save_job(job)
            return False
        for node in next_nodes:
            node.metadata = {**(node.metadata or {}), "plan_revision": revision}
            attach_display_plan(node)
            prepare_node_safety(node, job.user_id, job.job_id)
        job.nodes = next_nodes
        job.status = JobStatus.RUNNING
        job.error = None
        job.result = None
        job.routing["plan_revision"] = revision
        job.updated_at = time.time()
        await self._store.save_job(job)
        logger.info(
            "长清单任务进入下一批: job={} progress={}/{}",
            job.job_id[:8], progress["cursor"], progress["total"],
        )
        return True

    async def _maybe_replan_failed_job(self, job: Job, llm_api_key: str | None) -> bool:
        """Validate a result and evolve the visible plan with bounded retries.

        Replanning receives completed evidence and the concrete failed method.  This turns
        recovery into a stateful continuation instead of asking the planner to solve the
        original request again with no knowledge of what just happened.
        """
        if job.scene != "office" or job.status in {
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
            JobStatus.PAUSED,
        }:
            return False
        from app.agents.orchestration.safety import is_effectful
        from app.agents.orchestration.validation import FailureCategory, validate_job_outcome

        outcome = validate_job_outcome(job)
        job.routing = dict(job.routing or {})
        validation_audit = outcome.model_dump(mode="json")
        try:
            from app.core.agent_security import redact_server_text

            validation_audit["reason"] = redact_server_text(str(validation_audit.get("reason") or ""))
        except Exception:  # noqa: BLE001
            validation_audit["reason"] = str(validation_audit.get("reason") or "")[:500]
        job.routing["last_validation"] = validation_audit
        if outcome.valid:
            await self._store.save_job(job)
            return False
        # M0 的确定性文件交付失败（例如沙箱产物回传失败）不能被解释成
        # "换一个开放式计划"。保留原文件契约并终止，避免 M2 去分析无关文档。
        if not outcome.may_upgrade:
            if job.status != JobStatus.FAILED:
                job.status = JobStatus.FAILED
            job.error = outcome.reason or "任务未生成可交付产物"
            job.updated_at = time.time()
            job.routing["automatic_replan_blocked"] = "non_replanable_validation_failure"
            await self._store.save_job(job)
            return False
        # A task containing an external side effect must never be replayed
        # automatically, even when a later validation step fails.
        if any(is_effectful(node) and node.agent != "office_script" for node in job.nodes):
            job.routing["automatic_replan_blocked"] = "effectful_task"
            await self._store.save_job(job)
            return False

        current = ComplexityLevel(job.routing.get("level", "m2"))
        upgrade_count = int(job.routing.get("upgrade_count") or 0)
        replan_count = int(job.routing.get("replan_count") or 0)
        target = outcome.target_level
        if current == ComplexityLevel.M3 and outcome.category in {
            FailureCategory.CAPABILITY,
            FailureCategory.PLAN,
            FailureCategory.VALIDATION,
        }:
            target = ComplexityLevel.M3 if replan_count < 2 else None
        elif current == ComplexityLevel.M0 and target == ComplexityLevel.M1:
            # Escalate to a genuinely different planning method instead of
            # wrapping the failed deterministic primitive in another rule.
            target = ComplexityLevel.M2
        if target is None or upgrade_count >= 2 or replan_count >= 2:
            await self._store.save_job(job)
            return False

        context = self._job_plan_context.get(job.job_id)
        if not context:
            job.routing["automatic_replan_blocked"] = "context_unavailable"
            await self._store.save_job(job)
            return False
        planner_method = getattr(self._planner, "plan_for_level", None)
        if not callable(planner_method):
            job.routing["automatic_replan_blocked"] = "planner_not_level_aware"
            await self._store.save_job(job)
            return False

        def public_text(value: object, limit: int = 800) -> str:
            text = str(value or "").strip()
            try:
                from app.core.agent_security import redact_server_text

                text = redact_server_text(text)
            except Exception:  # noqa: BLE001
                pass
            return text[:limit]

        completed_evidence = []
        failed_evidence = []
        for node in job.nodes:
            result = node.result or {}
            if node.status == TaskStatus.COMPLETED:
                completed_evidence.append(
                    {
                        "step": public_text(node.name or node.agent, 120),
                        "result": public_text(result.get("content") or result.get("output"), 1200),
                        "outputs": [
                            public_text(item.get("name") if isinstance(item, dict) else item, 120)
                            for item in (result.get("outputs") or [])[:5]
                        ],
                    }
                )
            elif node.status == TaskStatus.FAILED:
                failed_evidence.append(
                    {
                        "step": public_text(node.name or node.agent, 120),
                        "method": public_text(
                            result.get("tool") or node.params.get("preferred_tool") or node.agent,
                            120,
                        ),
                        "error_code": public_text(node.error_code, 80),
                        "error": public_text(node.error, 500),
                    }
                )
        execution_feedback = {
            "instruction": (
                "这是同一任务的计划演进。保留已完成产物，只规划尚未完成的目标；"
                "不要重复已成功步骤；失败方法不得原样重试，应更换工具、参数或实现原理。"
            ),
            "completed": completed_evidence,
            "failed": failed_evidence,
        }
        evolution_context = (context.get("prior_summaries") or "").strip()
        evolution_context += "\n\n[当前任务执行反馈]\n" + json.dumps(
            execution_feedback, ensure_ascii=False, default=str
        )

        tree = await planner_method(
            target,
            context["user_id"],
            context["request"],
            context["scene"],
            context["project_id"],
            context["project_ids"],
            llm_api_key,
            context["clarification_answer"],
            context["office_docs"],
            evolution_context,
            bypass_fast_paths=True,
        )
        if tree.error or not tree.nodes:
            job.routing["replan_error"] = tree.error or tree.clarification or "未生成可执行步骤"
            await self._store.save_job(job)
            return False

        previous = {
            "level": current.value,
            "category": outcome.category.value,
            "steps": [
                {
                    "name": node.name,
                    "status": node.status.value,
                    "error_code": node.error_code,
                }
                for node in job.nodes
            ],
        }
        self._prefer_atomic_steps(tree.nodes, job.request)
        self._serialize_steps(tree.nodes)
        from app.agents.orchestration.dag import validate_planned_dag
        from app.agents.orchestration.presentation import attach_display_plan
        from app.agents.orchestration.safety import prepare_node_safety

        current_revision = int(job.routing.get("plan_revision") or 1)
        next_revision = current_revision + 1
        for node in tree.nodes:
            node.metadata = {**(node.metadata or {}), "plan_revision": next_revision}
            attach_display_plan(node)
            prepare_node_safety(node, job.user_id, job.job_id)
        errors = validate_planned_dag(tree.nodes, self._workers)
        if errors:
            job.routing["replan_error"] = "；".join(errors)[:500]
            await self._store.save_job(job)
            return False

        upgrades = list(job.routing.get("upgrades") or [])
        upgrades.append(
            {"from": current.value, "to": target.value, "reason": outcome.category.value}
        )
        attempts = list(job.routing.get("attempts") or [])
        attempts.append(previous)
        failed_names = [item["step"] for item in failed_evidence if item.get("step")]
        public_reason = (
            f"原计划中的“{'、'.join(failed_names[:2])}”未能完成，已根据执行结果更换方法。"
            if failed_names
            else "原计划未通过结果验证，已根据执行结果更换方法。"
        )
        plan_history = list(job.routing.get("plan_history") or [])
        plan_history.append(
            {
                "revision": current_revision,
                "plan_text": public_text(job.plan_text, 1000),
                "reason": public_reason,
                "changed_at": time.time(),
            }
        )
        job.routing.update(
            {
                "level": target.value,
                "mode": "react" if target == ComplexityLevel.M3 else "plan_execute",
                "upgrade_count": upgrade_count + int(target != current),
                "replan_count": replan_count + 1,
                "upgrades": upgrades,
                "attempts": attempts[-2:],
                "plan_revision": next_revision,
                "plan_history": plan_history[-3:],
                "plan_change_reason": public_reason,
            }
        )
        job.nodes = tree.nodes
        job.plan_text = tree.plan_text
        job.status = JobStatus.RUNNING
        job.error = None
        job.result = None
        job.updated_at = time.time()
        await self._store.save_job(job)
        try:
            from app.core.observability import inc_agent_replan

            inc_agent_replan(current.value, target.value, outcome.category.value)
        except Exception:  # noqa: BLE001
            pass
        logger.info(
            "办公任务失败升级: job={} {}->{} reason={}",
            job.job_id[:8], current.value, target.value, outcome.category.value,
        )
        return True

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
                    # Temporal 是运行时权威。终态必须回写 Redis，避免临时查询失败时
                    # 读回创建时的 RUNNING 快照，造成前端和用户限流永久卡住。
                    if job.status in {
                        JobStatus.COMPLETED,
                        JobStatus.FAILED,
                        JobStatus.CANCELLED,
                        JobStatus.INTERRUPTED,
                    }:
                        try:
                            await self._store.save_job(job)
                        except Exception as save_exc:  # noqa: BLE001
                            logger.debug("同步 Temporal 终态到 Redis 失败 {}: {}", job_id, save_exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("查询 Temporal 任务状态失败，回退快照 {}: {}", job_id, exc)
        if job is None:
            job = await self._store.get_job(job_id)
        if job is None:
            # 只兜底当前 API 实例中仍在执行的 legacy 任务，不能把内存作为长期
            # 状态库，也不会在实例重启后掩盖 Redis 的真实状态。
            live_job = self._live_jobs.get(job_id)
            if live_job is not None and live_job.status in {
                JobStatus.PENDING,
                JobStatus.RUNNING,
                JobStatus.PAUSED,
                JobStatus.WAITING_APPROVAL,
            }:
                logger.warning("Redis 未读到运行中任务快照，使用本地执行镜像: {}", job_id[:8])
                job = live_job.model_copy(deep=True)
        if job is None:
            return None
        # 办公短期记忆：任务终结时落一条"上一步做了什么"摘要（幂等）
        await self._record_office_summary(job)
        await self._record_job_metric(job)
        await self._learn_from_finished_job(job)
        if job.status in {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
        }:
            # Temporal 的终态只在查询时才能获知，因此此处同样释放容量；重复释放安全。
            await job_admission.release(job_id=job.job_id, user_id=job.user_id)
            self._job_plan_context.pop(job.job_id, None)
        return await self._attach_progress(job)

    async def _learn_from_finished_job(self, job: Job) -> None:
        """Commit successful plans only; failed/cancelled work never pollutes reuse."""
        pending = self._pending_plan_cache.get(job.job_id)
        if job.status != JobStatus.COMPLETED or not pending:
            if job.status in {
                JobStatus.FAILED,
                JobStatus.CANCELLED,
                JobStatus.INTERRUPTED,
            }:
                self._pending_plan_cache.pop(job.job_id, None)
            return
        key, office_docs = pending
        stored = await self._plan_cache.put(key, job.nodes, office_docs, job.plan_text)
        if stored:
            self._pending_plan_cache.pop(job.job_id, None)
            try:
                from app.core.observability import inc_plan_cache

                inc_plan_cache("stored")
            except Exception:  # noqa: BLE001
                pass

    def _discard_pending_learning(self, job_id: str) -> None:
        self._job_plan_context.pop(job_id, None)
        self._pending_plan_cache.pop(job_id, None)

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
        # 同步中止正在等待的客户端 MCP 调用；远端兼容服务器会收到标准
        # notifications/cancelled，本地不可取消的工具也至少不再阻塞调度器。
        try:
            from app.agents.mcp.manager import cancel_task

            await cancel_task(job_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("取消任务时通知 MCP 失败 {}: {}", job_id, exc)
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
                    await job_admission.release(job_id=job.job_id, user_id=job.user_id)
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
        await job_admission.release(job_id=job.job_id, user_id=job.user_id)
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
