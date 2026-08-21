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
    reconcile_structured_manifest,
    schedule_manifest_route_upgrades,
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
    ``manifest_temporal`` 仅接收显式任务清单，普通办公任务仍走动态 DAG。
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
            settings.AGENT_ORCHESTRATION == "manifest_temporal"
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
        self._admission_heartbeats: dict[str, asyncio.Task] = {}

    def _start_admission_heartbeat(self, job_id: str, user_id: str) -> None:
        current = self._admission_heartbeats.get(job_id)
        if current is not None and not current.done():
            return

        async def heartbeat() -> None:
            interval = max(10.0, min(60.0, settings.AGENT_ADMISSION_LEASE_SECONDS / 3))
            try:
                while True:
                    await asyncio.sleep(interval)
                    if not await job_admission.renew(job_id, user_id):
                        logger.error("办公任务准入租约丢失，停止任务: {}", job_id[:8])
                        job = await self._store.get_job(job_id)
                        if job and job.status in {
                            JobStatus.PENDING,
                            JobStatus.RUNNING,
                            JobStatus.PAUSED,
                            JobStatus.WAITING_APPROVAL,
                            JobStatus.CONTINUING,
                        }:
                            job.status = JobStatus.INTERRUPTED
                            job.error = "任务运行租约已失效，为避免并发超限已自动停止。"
                            job.updated_at = time.time()
                            await self._store.save_job(job)
                            task = self._tasks.get(job_id)
                            if task is not None and not task.done():
                                task.cancel()
                        return
            except asyncio.CancelledError:
                return
            finally:
                current_task = asyncio.current_task()
                if self._admission_heartbeats.get(job_id) is current_task:
                    self._admission_heartbeats.pop(job_id, None)

        self._admission_heartbeats[job_id] = asyncio.create_task(heartbeat())

    async def _stop_admission_heartbeat(self, job_id: str) -> None:
        task = self._admission_heartbeats.pop(job_id, None)
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

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

    async def _record_office_task_index(self, job: Job) -> None:
        """Write the durable recent-task index without delaying task completion."""
        if job.scene != "office" or job.status not in {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
        }:
            return
        try:
            from app.core.database import async_session_factory
            from app.services.office_task_memory import upsert_office_task_index

            async with async_session_factory() as session:
                await upsert_office_task_index(session, job)
        except Exception as exc:  # noqa: BLE001
            # Migration rollout must not turn a finished office task back into
            # an error.  The index is a recall optimization, never task state.
            logger.debug("写入办公近期任务索引失败 {}: {}", job.job_id[:8], exc)

    async def _load_office_recall_context(
        self, user_id: str, request: str, conversation_id: str | None
    ) -> str:
        """Recall prior work only after an explicit historical reference."""
        try:
            from app.services.office_task_memory import needs_office_task_recall, recall_office_tasks

            if not needs_office_task_recall(request):
                return ""
            from app.core.database import async_session_factory

            async with async_session_factory() as session:
                recalled = await recall_office_tasks(
                    session,
                    user_id=user_id,
                    request=request,
                    conversation_id=conversation_id,
                )
            return recalled.context
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取办公近期任务索引失败: {}", exc)
            return ""

    async def _load_office_presentation_preferences(self, user_id: str) -> str:
        """Load only whitelisted chat preferences for final delivery styling.

        This value never enters the planner, node parameters, safety gates or
        task-memory context.  It is passed only to the final answer formatter.
        """
        try:
            from app.core.database import async_session_factory
            from app.services.office_task_memory import get_office_presentation_preferences

            async with async_session_factory() as session:
                return await get_office_presentation_preferences(session, user_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取办公展示偏好失败: {}", exc)
            return ""

    async def _verified_office_docs(
        self, user_id: str, request: str, office_docs: list[dict] | None
    ) -> list[dict]:
        """Re-resolve client attachment metadata before it reaches a planner."""
        if not office_docs:
            return []
        try:
            from app.services.office_docs import ensure_session
        except Exception:  # noqa: BLE001
            return []
        # When the user named an attachment, validate that exact attachment
        # first.  This avoids needless session restores for unrelated uploads
        # and keeps the selected-file contract precise.
        candidates = office_docs
        try:
            from app.agents.orchestration.planner import select_named_office_documents

            selected, _unresolved, has_named = select_named_office_documents(request, office_docs)
            if has_named:
                candidates = selected
        except Exception:  # noqa: BLE001
            pass
        verified: list[dict] = []
        seen: set[str] = set()
        for item in candidates[:12]:
            if not isinstance(item, dict):
                continue
            doc_id = str(item.get("doc_id") or "")[:64]
            if not doc_id or doc_id in seen:
                continue
            try:
                meta = await ensure_session(user_id, doc_id)
            except (LookupError, ValueError):
                logger.info("办公附件未通过会话归属校验: {}", doc_id[:8])
                continue
            seen.add(doc_id)
            verified.append(
                {
                    "doc_id": doc_id,
                    "filename": str(meta.get("filename") or "")[:500],
                    "kind": str(meta.get("kind") or "text")[:20],
                }
            )
        return verified

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
        # 默认不为新任务加载旧任务。只有用户明确回指历史工作时才查持久化
        # 索引；Redis 摘要仅是迁移期间的无结果兼容回退。
        prior_summaries = ""
        if scene == "office":
            # Client-provided attachment metadata is not an authorization
            # record.  All references below use the session metadata resolved
            # for this user, and only this compact identity is retained on Job.
            office_docs = await self._verified_office_docs(user_id, request, office_docs)
            prior_summaries = await self._load_office_recall_context(
                user_id, request, conversation_id
            )
            if not prior_summaries and conversation_id:
                try:
                    from app.services.office_task_memory import needs_office_task_recall

                    if needs_office_task_recall(request):
                        prior_summaries = await self._load_office_summaries(conversation_id)
                except Exception:  # noqa: BLE001
                    pass
        presentation_preferences = (
            await self._load_office_presentation_preferences(user_id)
            if scene == "office"
            else ""
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
                        # Every authorized list is normalized into atomic JSON.
                        # Explicit bullets are a safe fallback, not a reason to
                        # skip dependency discovery: unrelated items should not
                        # be artificially chained merely due to their layout.
                        parsed_items = parse_task_manifest(source_text)
                        try:
                            structured_items = await extract_natural_language_manifest(
                                source_text,
                                user_id=user_id,
                                api_key=llm_api_key,
                                source_label=source_label,
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.info("清单结构化模型不可用，按显式顺序保守执行: {}", exc)
                            structured_items = []
                        # The model's JSON can enrich an explicitly numbered
                        # checklist with dependencies/subgraphs, but never
                        # becomes the source of truth for its membership.  A
                        # partial response must not silently omit user work.
                        manifest_items, manifest_cleaning = reconcile_structured_manifest(
                            parsed_items, structured_items
                        )
                        if not manifest_items:
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
            if manifest["estimated_tokens"] > settings.AGENT_MANIFEST_TOKEN_BUDGET:
                from app.agents.orchestration.planner import TaskTree

                tree = TaskTree(
                    nodes=[],
                    clarification=(
                        f"这份清单预估会消耗约 {manifest['estimated_tokens']} token，超过当前单次任务预算。"
                        "请拆分清单后重试，或明确回复“确认执行该清单”以授权较高预算。"
                    ),
                )
                routing = {
                    "level": "manifest", "mode": "manifest_budget_confirmation",
                    "cache_hit": False, "plan_revision": 1,
                    "manifest_source": manifest_source,
                    "estimated_tokens": manifest["estimated_tokens"],
                    "manifest_cleaning": manifest_cleaning,
                }
            else:
                tree = TaskTree(
                    nodes=materialize_manifest_batch(manifest),
                    plan_text=(
                        f"已识别 {len(manifest_items)} 项清单；每项将按直接生成、脚本、检索或智能体通道执行，"
                        f"每批 {manifest['batch_size']} 项。"
                    ),
                )
                routing = {
                    "level": "manifest",
                    "mode": "four_channel_manifest",
                    "cache_hit": False,
                    "plan_revision": 1,
                    "manifest": manifest,
                    "manifest_progress": manifest_progress(manifest),
                    "estimated_tokens": manifest["estimated_tokens"],
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
        self._adapt_unavailable_manifest_workers(tree.nodes)
        # A manifest already carries explicit dependency edges.  Do not flatten
        # them into a global chain: independent items must be allowed to use
        # their own channel capacity, while resource claims still serialize
        # conflicting effects.
        if not routing.get("manifest"):
            self._serialize_steps(tree.nodes)
        if scene == "office":
            routing["input_refs"] = [
                {
                    "doc_id": str(item.get("doc_id") or ""),
                    "filename": str(item.get("filename") or "")[:500],
                    "kind": str(item.get("kind") or "")[:20],
                }
                for item in office_docs or []
                if item.get("doc_id")
            ]
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
        job.execution_id = job.job_id
        job.root_execution_id = job.job_id
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
                "presentation_preferences": presentation_preferences,
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
        # Complex ordinary tasks no longer keep the whole planned graph in the
        # active Job snapshot.  Persist the logical graph externally and start
        # only its ready frontier.  Explicit manifests already own the same
        # lifecycle through their dedicated controller.
        if (
            scene == "office"
            and settings.AGENT_LOGICAL_PLAN_ENABLED
            and not routing.get("manifest")
            and len(job.nodes) >= settings.AGENT_LOGICAL_PLAN_MIN_NODES
        ):
            from app.agents.orchestration.logical_plan import (
                create_logical_plan,
                logical_plan_progress,
                materialize_frontier,
                save_logical_plan,
            )

            logical_plan = await create_logical_plan(user_id, job.nodes)
            if logical_plan["budget"]["estimated_total"] > logical_plan["budget"]["limit"]:
                job.status = JobStatus.COMPLETED
                job.result = {
                    "type": "clarification",
                    "question": (
                        f"该任务预估消耗约 {logical_plan['budget']['estimated_total']} token，"
                        "超过当前单次任务预算。请拆分任务后重试，或明确确认较高预算。"
                    ),
                }
                job.nodes = []
                routing["logical_plan"] = {
                    "plan_id": logical_plan["plan_id"],
                    "state": "budget_confirmation",
                    "progress": logical_plan_progress(logical_plan),
                }
                await self._store.create_job(job)
                await job_admission.release(token=admission_token)
                self._discard_pending_learning(job.job_id)
                return job
            frontier = materialize_frontier(logical_plan)
            if not frontier:
                job.status = JobStatus.FAILED
                job.error = "任务计划没有可执行前沿，已停止以避免无效调度。"
                job.nodes = []
                routing["logical_plan"] = {
                    "plan_id": logical_plan["plan_id"],
                    "state": "blocked",
                    "progress": logical_plan_progress(logical_plan),
                }
                await self._store.create_job(job)
                await job_admission.release(token=admission_token)
                self._discard_pending_learning(job.job_id)
                return job
            await save_logical_plan(user_id, logical_plan)
            job.nodes = frontier
            routing["logical_plan"] = {
                "plan_id": logical_plan["plan_id"],
                "revision": logical_plan["revision"],
                "frontier_size": len(frontier),
                "progress": logical_plan_progress(logical_plan),
                "estimated_tokens": logical_plan["budget"]["estimated_total"],
            }
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
        self._start_admission_heartbeat(job.job_id, user_id)
        try:
            # The old static Temporal workflow is frozen.  Only explicitly
            # authorized rolling manifests may opt into the new runtime; the
            # job retains this decision in Redis for every later control call.
            if (
                job.routing.get("manifest")
                and self._temporal_mode
                and self._can_run_manifest_temporal(job)
                and await self._probe_temporal()
            ):
                try:
                    await self._submit_manifest_temporal(job, llm_api_key)
                    logger.info(
                        "清单任务已提交(Temporal): {} | agent={} request={}",
                        job.job_id[:8],
                        [n.agent for n in job.nodes],
                        request[:40],
                    )
                    return job
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Temporal 清单提交失败，回退自建 DAG: {} | {}", job.job_id[:8], exc)
                    job.routing = {
                        **(job.routing or {}),
                        "runtime": "legacy",
                        "temporal_submit_error": str(exc)[:200],
                    }

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
            await self._stop_admission_heartbeat(job.job_id)
            raise

    @staticmethod
    def _can_run_manifest_temporal(job: Job) -> bool:
        """Gate phase-2 rollout to read-only manifest channels.

        The rolling workflow has durable batch execution and recovery, but L2
        approvals and L3 replacement subgraphs are migrated in the next phase.
        Routing an effectful/script/agent item here before then would silently
        weaken those semantics, so it stays on the proven dynamic DAG path.
        """
        manifest = (job.routing or {}).get("manifest")
        if not isinstance(manifest, dict):
            return False
        for item in list(manifest.get("items") or []):
            route = str(item.get("route") or item.get("estimated_type") or "")
            if route not in {"direct_llm", "rag"}:
                return False
            if list(item.get("subtasks") or []):
                return False
        return True

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

    def _adapt_unavailable_manifest_workers(self, nodes) -> None:
        """Allow narrow test/degraded worker pools without changing prod routes.

        Production registers all four channel workers.  A deliberately trimmed
        deployment may only retain ``react_step``; it is safer to execute the
        already-authorized atom through that bounded worker than to fail the
        entire manifest during static validation.  The original route remains
        in metadata for audit and capacity reporting.
        """
        if "react_step" not in self._workers:
            return
        for node in nodes:
            if node.agent not in self._workers and node.agent != "collect_results":
                node.metadata = {**(node.metadata or {}), "route_worker_fallback": node.agent}
                node.agent = "react_step"
                node.params.setdefault("max_rounds", 2)
            if node.agent == "collect_results" and node.agent not in self._workers:
                # Collection is optimization/observability, never a reason to
                # make an otherwise completed checklist unavailable.
                node.metadata = {**(node.metadata or {}), "manifest_collect_skipped": True}
                node.agent = "react_step"
                node.params = {
                    "instruction": "汇集并简要列出本批清单的已完成、失败和取消结果。",
                    "max_rounds": 1,
                }

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
        """Deprecated frozen static Temporal submit path. Do not call for new jobs."""
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

    async def _submit_manifest_temporal(self, job: Job, llm_api_key: str | None) -> None:
        """Persist first, then submit a tiny rolling-manifest Workflow input."""
        from app.agents.orchestration.temporal.client import start_manifest_workflow, store_byok_key

        job.routing = {
            **(job.routing or {}),
            "runtime": "manifest_temporal",
            "runtime_version": 1,
        }
        # Redis remains the complete job state authority. The workflow sees
        # only a job reference, so neither documents, tool output nor BYOK
        # credentials are copied into Temporal history.
        await self._store.create_job(job)
        if llm_api_key:
            await store_byok_key(job.job_id, llm_api_key)
        await start_manifest_workflow(
            {
                "job_id": job.job_id,
                "heartbeat_seconds": settings.TEMPORAL_ACTIVITY_HEARTBEAT_SECONDS,
                "batch_timeout_seconds": max(
                    300,
                    int(settings.AGENT_NODE_TIMEOUT_SECONDS) * max(2, int((job.routing.get("manifest") or {}).get("batch_size") or 1)),
                ),
                "continue_after_batches": settings.TEMPORAL_MANIFEST_CONTINUE_AS_NEW_BATCHES,
            },
            job.job_id,
        )

    @staticmethod
    def _is_manifest_temporal_job(job: Job | None) -> bool:
        return bool(job and str((job.routing or {}).get("runtime") or "") == "manifest_temporal")

    @staticmethod
    def _ancestor_ids(nodes, node_id: str) -> set[str]:
        """Return the selected node's transitive dependency prefix."""
        by_id = {node.id: node for node in nodes}
        seen: set[str] = set()
        pending = list((by_id.get(node_id).depends_on if by_id.get(node_id) else []))
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            node = by_id.get(current)
            if node is None:
                continue
            seen.add(current)
            pending.extend(node.depends_on)
        return seen

    @staticmethod
    def _descendant_ids(nodes, node_id: str) -> set[str]:
        """Return the selected node and every node that depends on it."""
        children: dict[str, list[str]] = {node.id: [] for node in nodes}
        for node in nodes:
            for dependency in node.depends_on:
                if dependency in children:
                    children[dependency].append(node.id)
        seen = {node_id}
        pending = [node_id]
        while pending:
            current = pending.pop()
            for child in children.get(current, []):
                if child not in seen:
                    seen.add(child)
                    pending.append(child)
        return seen

    async def fork_job(
        self,
        job_id: str,
        *,
        node_id: str,
        params: dict | None = None,
        instruction: str | None = None,
        llm_api_key: str | None = None,
    ) -> Job:
        """Create an immutable execution branch from a safe completed prefix.

        Forks are intentionally a control-plane feature, independent from the
        current execution backend. The new Job contains only opaque result
        references for upstream nodes; their bodies are owner-scoped and are
        loaded only when a dependent node executes.
        """
        source = await self._store.get_job(job_id)
        if source is None:
            raise RuntimeError("源任务不存在")
        if source.status not in {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
        }:
            raise RuntimeError("只能从已结束的任务创建分支")
        if self._is_manifest_temporal_job(source) or isinstance((source.routing or {}).get("manifest"), dict):
            raise RuntimeError("滚动清单的历史节点仍在压缩归档，暂不支持从单个节点回放")
        by_id = {node.id: node for node in source.nodes}
        target = by_id.get(node_id)
        if target is None:
            raise RuntimeError("回放节点不存在")
        prefix = self._ancestor_ids(source.nodes, node_id)
        rerun_ids = self._descendant_ids(source.nodes, node_id)
        from app.agents.orchestration.safety import is_effectful, prepare_node_safety
        from app.agents.orchestration.execution_lineage import ensure_node_result_ref, resolve_result_ref

        # Anything outside the selected downstream subgraph is retained as a
        # completed immutable prefix. This prevents an unrelated completed
        # email/file write from being replayed just because the user reruns a
        # parallel analysis node.
        retained_ids = {node.id for node in source.nodes if node.id not in rerun_ids}
        # Forking is a forward-only operation in v1.  An effect in the
        # selected node's dependency prefix has already changed the outside
        # world, so a new branch cannot truthfully represent an alternative
        # history from before that point.  Compensation is deliberately a
        # later, explicit workflow rather than an implicit replay side effect.
        committed_prefix_effects = [
            by_id[ancestor_id]
            for ancestor_id in prefix
            if by_id[ancestor_id].effect_status == "committed"
        ]
        if committed_prefix_effects:
            names = ", ".join(node.name or node.id for node in committed_prefix_effects[:3])
            raise RuntimeError(f"回放点上游包含已提交的副作用步骤（{names}），第一版不支持跨该边界分支")
        for retained_id in retained_ids:
            upstream = by_id[retained_id]
            if upstream.status != TaskStatus.COMPLETED:
                raise RuntimeError("分支外存在未完成步骤，不能安全复用执行前缀")
            # A completed effect outside the rerun subgraph is safe to retain
            # as history. It is never replayed and has no result body copied.
            result_ref = await ensure_node_result_ref(source.user_id, upstream)
            if not result_ref or not await resolve_result_ref(source.user_id, result_ref):
                raise RuntimeError("已完成步骤的结果引用已过期或不可验证，不能安全创建分支")
        # The selected node itself may be an effect only when it did not
        # previously commit. A committed selected node means the user is
        # trying to cross an irreversible boundary, which v1 deliberately
        # rejects instead of guessing a compensation action.
        if is_effectful(target) and target.effect_status == "committed":
            raise RuntimeError("选中的节点已产生副作用，第一版不支持从该节点重新分支")
        if retained_ids:
            await self._store.save_job(source)

        admission_token = str(uuid.uuid4())
        try:
            await job_admission.reserve(admission_token)
            active_statuses = {
                JobStatus.PENDING,
                JobStatus.RUNNING,
                JobStatus.PAUSED,
                JobStatus.WAITING_APPROVAL,
            }
            active_jobs = [job for job in await self.list_jobs(source.user_id, 50) if job.status in active_statuses]
            if len(active_jobs) >= settings.AGENT_USER_ACTIVE_JOB_LIMIT:
                raise UserJobLimitError("当前有任务正在进行中，请等待其完成后再创建分支")

            new_job_id = str(uuid.uuid4())
            forked_nodes = [node.model_copy(deep=True) for node in source.nodes]
            for node in forked_nodes:
                node.metadata = dict(node.metadata or {})
                if node.id in retained_ids:
                    node.status = TaskStatus.COMPLETED
                    node.result = None
                    node.error = None
                    node.error_code = None
                    node.started_at = None
                    node.completed_at = None
                    node.retries = 0
                    node.metadata["replay_prefix"] = True
                    continue
                node.status = TaskStatus.PENDING
                node.result = None
                node.error = None
                node.error_code = None
                node.retries = 0
                node.started_at = None
                node.completed_at = None
                node.effect_status = None
                node.idempotency_key = None
                node.metadata.pop("result_ref", None)
                node.metadata.pop("dependency_results", None)
                node.metadata.pop("awaiting_approval", None)
                node.metadata.pop("approval_fingerprint", None)
                node.metadata.pop("confirmed_tools", None)
                node.metadata.pop("confirmed_tool_calls", None)
            forked_target = next(node for node in forked_nodes if node.id == node_id)
            if params:
                forked_target.params = {**forked_target.params, **dict(params)}
            if instruction is not None:
                forked_target.params = {**forked_target.params, "instruction": str(instruction)[:4000]}

            routing = dict(source.routing or {})
            routing.update({
                "runtime": "legacy",
                "execution_kind": "fork",
                "fork": {
                    "parent_execution_id": source.execution_id or source.job_id,
                    "parent_job_id": source.job_id,
                    "forked_from_node_id": node_id,
                    "prefix_node_ids": sorted(prefix),
                    "created_at": time.time(),
                },
            })
            job = Job(
                job_id=new_job_id,
                execution_id=new_job_id,
                parent_execution_id=source.execution_id or source.job_id,
                root_execution_id=source.root_execution_id or source.execution_id or source.job_id,
                forked_from_node_id=node_id,
                user_id=source.user_id,
                user_role=source.user_role,
                request=source.request,
                scene=source.scene,
                conversation_id=source.conversation_id,
                status=JobStatus.RUNNING,
                nodes=forked_nodes,
                plan_text=source.plan_text,
                routing=routing,
            )
            for node in job.nodes:
                prepare_node_safety(node, job.user_id, job.job_id)
            from app.agents.orchestration.dag import validate_planned_dag

            errors = validate_planned_dag(job.nodes, self._workers)
            if errors:
                raise RuntimeError("分支计划校验失败：" + "；".join(errors[:3]))
            await job_admission.promote(admission_token, job.job_id, job.user_id)
            self._start_admission_heartbeat(job.job_id, job.user_id)
            await self._store.create_job(job)
            self._live_jobs[job.job_id] = job
            if llm_api_key:
                self._job_api_keys[job.job_id] = llm_api_key
            self._tasks[job.job_id] = asyncio.create_task(self._run_job(job.job_id))
            return job
        except UserJobLimitError:
            await job_admission.release(token=admission_token)
            raise
        except Exception:
            await job_admission.release(token=admission_token)
            raise

    async def _run_job(self, job_id: str) -> None:
        """legacy 后台执行：校验 DAG → 拓扑执行 → 汇总状态."""
        # Keep the ephemeral BYOK key while an L2 approval gate is waiting;
        # the resumed legacy worker must use the same user-selected model.
        llm_api_key = self._job_api_keys.get(job_id)
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
                    concurrency=settings.AGENT_NODE_CONCURRENCY,
                    llm_api_key=llm_api_key,
                )
                job = await self._store.get_job(job_id) or job
                self._live_jobs[job_id] = job
                if await self._continue_manifest_job(job):
                    job = await self._store.get_job(job_id) or job
                    self._live_jobs[job_id] = job
                    continue
                if await self._continue_logical_plan(job):
                    job = await self._store.get_job(job_id) or job
                    self._live_jobs[job_id] = job
                    continue
                if not await self._maybe_replan_failed_job(job, llm_api_key):
                    break
                job = await self._store.get_job(job_id) or job
                self._live_jobs[job_id] = job
                # L2 has already converged to a user-facing clarification or
                # paused at an approval gate.  It is not a new execution
                # graph, so do not immediately resubmit the same node.
                if job.status in {
                    JobStatus.COMPLETED,
                    JobStatus.FAILED,
                    JobStatus.CANCELLED,
                    JobStatus.INTERRUPTED,
                    JobStatus.WAITING_APPROVAL,
                }:
                    break
            job = await self._store.get_job(job_id)
            # A failed node/replan must always converge the job itself.  A
            # terminal task is essential for UI recovery and admission-slot
            # release; leaving RUNNING here used to pin the stop button.
            if job and job.status not in {
                JobStatus.COMPLETED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
                JobStatus.INTERRUPTED,
                JobStatus.WAITING_APPROVAL,
            }:
                job.status = JobStatus.FAILED
                job.error = job.error or "办公任务未能收敛，已自动停止。"
                job.updated_at = time.time()
                await self._store.save_job(job)
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
                                "presentation_preferences": (
                                    self._job_plan_context.get(job.job_id) or {}
                                ).get("presentation_preferences", ""),
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
            finished = await self._store.get_job(job_id)
            waiting_approval = bool(finished and finished.status == JobStatus.WAITING_APPROVAL)
            if not waiting_approval:
                self._job_api_keys.pop(job_id, None)
                self._live_jobs.pop(job_id, None)
            # legacy 执行器的 finally 是最可靠的容量释放点（包括取消/异常）。
            try:
                finished = finished or await self._store.get_job(job_id)
                if finished and finished.status in {
                    JobStatus.COMPLETED,
                    JobStatus.FAILED,
                    JobStatus.CANCELLED,
                    JobStatus.INTERRUPTED,
                }:
                    await self._record_office_summary(finished)
                    await self._record_office_task_index(finished)
                    await job_admission.release(job_id=job_id, user_id=finished.user_id)
                    await self._stop_admission_heartbeat(job_id)
                if finished:
                    await self._learn_from_finished_job(finished)
            except Exception as exc:  # noqa: BLE001
                logger.debug("释放办公任务准入槽失败 {}: {}", job_id, exc)
            if not waiting_approval:
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
        # First commit every settled item in this window.  If one read-only
        # atom needs a broader channel, its already successful siblings must
        # remain committed and must never be replayed with the replacement.
        apply_manifest_batch_results(manifest, job.nodes)
        # A direct answer may discover that the answer is grounded in private
        # material; a RAG answer may discover that the remaining request needs
        # stateful/system work.  Replace only that read-only atom, retain its
        # audit trail, and do not replay effects or deterministic file writes.
        upgrades = schedule_manifest_route_upgrades(manifest, job.nodes)
        if upgrades:
            revision = int(job.routing.get("plan_revision") or 1) + 1
            job.nodes = materialize_manifest_batch(manifest, revision=revision)
            if not job.nodes:
                job.status = JobStatus.FAILED
                job.error = "任务通道升级后没有可执行步骤"
                job.updated_at = time.time()
                await self._store.save_job(job)
                return False
            from app.agents.orchestration.presentation import attach_display_plan
            from app.agents.orchestration.safety import prepare_node_safety

            for node in job.nodes:
                node.metadata = {**(node.metadata or {}), "plan_revision": revision}
                attach_display_plan(node)
                prepare_node_safety(node, job.user_id, job.job_id)
            job.routing = dict(job.routing or {})
            job.routing["manifest"] = manifest
            job.routing["plan_revision"] = revision
            job.routing["manifest_route_upgrades"] = list(job.routing.get("manifest_route_upgrades") or []) + upgrades
            job.status = JobStatus.RUNNING
            job.error = None
            job.result = None
            job.updated_at = time.time()
            await self._store.save_job(job)
            try:
                from app.core.observability import inc_manifest_route_upgrade

                for upgrade in upgrades:
                    inc_manifest_route_upgrade(upgrade["from"], upgrade["to"], upgrade["reason"])
            except Exception:  # noqa: BLE001
                pass
            logger.info("清单原子任务通道升级: job={} upgrades={}", job.job_id[:8], upgrades)
            return True
        # Reroute nodes have now settled. Return to the ordinary rolling
        # phase before testing completion, otherwise the final successful
        # reroute would bypass the collector/final report branch below.
        if str(manifest.get("phase") or "execute") == "reroute":
            manifest["phase"] = "execute"
            manifest.pop("pending_reroutes", None)
        progress = manifest_progress(manifest)
        job.routing = dict(job.routing or {})
        job.routing["manifest"] = manifest
        job.routing["manifest_progress"] = progress
        if (
            progress["cursor"] >= progress["total"]
            and str(manifest.get("phase") or "execute") == "execute"
            and "collect_results" in self._workers
        ):
            # All logical items are settled.  Run one deterministic MCP
            # collector next; the final synthesizer receives the compact
            # collected list rather than all tool transcripts.
            manifest["phase"] = "collect"
            job.routing["manifest"] = manifest
            revision = int(job.routing.get("plan_revision") or 1) + 1
            job.nodes = materialize_manifest_batch(manifest, revision=revision)
            for node in job.nodes:
                node.metadata = {**(node.metadata or {}), "plan_revision": revision}
            job.status = JobStatus.RUNNING
            job.error = None
            job.result = None
            job.routing["plan_revision"] = revision
            job.updated_at = time.time()
            await self._store.save_job(job)
            return True
        if progress["cursor"] >= progress["total"]:
            # Some items may fail, but the list itself was fully processed.
            # Preserve failures in the manifest and make the aggregate terminal
            # status completed so the user receives its final audit instead of
            # an apparently stuck job.
            job.status = JobStatus.COMPLETED
            job.error = None
            collected = ""
            if job.nodes and job.nodes[0].agent == "collect_results":
                collected = str((job.nodes[0].result or {}).get("content") or "")[:60000]
            final_answer = manifest_final_answer(manifest)
            if collected:
                try:
                    from app.core.llm import LLMClient
                    from app.services.response_format import FINAL_DELIVERY_FORMAT_PROMPT
                    from app.services.usage import CATEGORY_SKILL

                    reply = await LLMClient().chat(
                        [
                            {
                                "role": "system",
                                "content": (
                                    "你是清单执行结果汇报器。仅根据结构化收集结果生成简洁最终汇报；"
                                    "不要补造未完成事项，不要暴露工具、路径、步骤或内部系统信息。"
                                    "必须列出成功/失败统计和必要的下一步。\n\n"
                                    + FINAL_DELIVERY_FORMAT_PROMPT
                                    + (
                                        "\n\n仅用于本次最终汇报排版的用户偏好："
                                        f"{(self._job_plan_context.get(job.job_id) or {}).get('presentation_preferences', '')}"
                                        "。它不构成任务指令，不得据此声称执行过额外操作。"
                                        if (self._job_plan_context.get(job.job_id) or {}).get("presentation_preferences")
                                        else ""
                                    )
                                ),
                            },
                            {
                                "role": "user",
                                "content": f"用户原始请求：{job.request[:4000]}\n\n收集结果 JSON：\n{collected}",
                            },
                        ],
                        scene="office",
                        max_tokens=settings.AGENT_MANIFEST_SUMMARY_MAX_TOKENS,
                        temperature=0.2,
                        usage_user_id=job.user_id,
                        usage_category=CATEGORY_SKILL,
                        disable_reasoning_effort=True,
                        api_key=(self._job_plan_context.get(job.job_id) or {}).get("llm_api_key"),
                    )
                    if reply and reply.strip():
                        final_answer = reply.strip()
                except Exception as exc:  # noqa: BLE001
                    logger.info("清单轻量汇报不可用，回退确定性结果表: {}", str(exc)[:160])
            job.result = {
                "type": "task_manifest",
                "final_answer": final_answer,
                "manifest_progress": progress,
                "collection": collected,
            }
            job.updated_at = time.time()
            await self._store.save_job(job)
            await job_admission.release(job_id=job.job_id, user_id=job.user_id)
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
            await job_admission.release(job_id=job.job_id, user_id=job.user_id)
            return False
        for node in next_nodes:
            node.metadata = {**(node.metadata or {}), "plan_revision": revision}
        self._adapt_unavailable_manifest_workers(next_nodes)
        for node in next_nodes:
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

    async def _continue_logical_plan(self, job: Job) -> bool:
        """Commit a single ordinary-DAG frontier and materialize the next one."""
        pointer = (job.routing or {}).get("logical_plan")
        if not isinstance(pointer, dict) or not pointer.get("plan_id"):
            return False
        if job.status in {JobStatus.CANCELLED, JobStatus.INTERRUPTED, JobStatus.PAUSED, JobStatus.WAITING_APPROVAL}:
            return False
        from app.agents.orchestration.logical_plan import (
            commit_frontier_results,
            load_logical_plan,
            logical_plan_progress,
            materialize_frontier,
            save_logical_plan,
        )

        plan = await load_logical_plan(job.user_id, str(pointer["plan_id"]))
        if not plan:
            job.status = JobStatus.FAILED
            job.error = "逻辑计划状态不可用，已停止以避免重复执行。"
            await self._store.save_job(job)
            return False
        await commit_frontier_results(job.user_id, plan, job.nodes)
        progress = logical_plan_progress(plan)
        job.routing = dict(job.routing or {})
        job.routing["logical_plan"] = {
            **pointer,
            "revision": plan.get("revision", 1),
            "progress": progress,
            "used_estimated_tokens": (plan.get("budget") or {}).get("used_estimated", 0),
        }
        # A failed materialized node is handed to L2/L3 arbitration below.
        # Do not materialize a tail before the orchestrator has decided whether
        # it must be replaced.
        if progress["failed"]:
            await self._store.save_job(job)
            return False
        if progress["completed"] >= progress["total"]:
            job.status = JobStatus.COMPLETED
            job.error = None
            job.updated_at = time.time()
            await save_logical_plan(job.user_id, plan)
            await self._store.save_job(job)
            return False
        frontier = materialize_frontier(plan)
        if not frontier:
            budget = plan.get("budget") or {}
            if int(budget.get("used_estimated") or 0) + int(budget.get("reserved") or 0) >= int(budget.get("limit") or 0):
                job.error = "任务执行预算已用尽，未执行的后续步骤已停止。"
            else:
                job.error = "逻辑计划没有满足依赖的后续步骤，已停止以避免无效调度。"
            job.status = JobStatus.FAILED
            job.updated_at = time.time()
            await save_logical_plan(job.user_id, plan)
            await self._store.save_job(job)
            return False
        await save_logical_plan(job.user_id, plan)
        job.nodes = frontier
        job.status = JobStatus.RUNNING
        job.error = None
        job.result = None
        job.updated_at = time.time()
        await self._store.save_job(job)
        return True

    async def _maybe_replan_logical_plan(self, job: Job, llm_api_key: str | None) -> bool:
        """Arbitrate a failed rolling-DAG frontier without mutating it in place.

        The active ``Job.nodes`` list is only an execution window.  L2 may
        resume its existing materialized node, while L3 replaces the external
        plan's uncommitted suffix and then materializes a fresh frontier.  A
        worker never receives the external plan and therefore cannot extend or
        rewrite the outer graph by itself.
        """
        pointer = (job.routing or {}).get("logical_plan")
        if not isinstance(pointer, dict) or not pointer.get("plan_id"):
            return False
        if job.scene != "office" or job.status in {
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
            JobStatus.PAUSED,
        }:
            return False

        from app.agents.orchestration.logical_plan import (
            load_logical_plan,
            logical_plan_progress,
            materialize_frontier,
            replace_unfinished_tail,
            save_logical_plan,
        )

        plan = await load_logical_plan(job.user_id, str(pointer["plan_id"]))
        if not plan:
            job.status = JobStatus.FAILED
            job.error = "逻辑计划状态不可用，无法安全恢复失败步骤。"
            await self._store.save_job(job)
            return False

        # L2 stays in the prebuilt approval/clarification controller.  An
        # approved node remains the same logical node, so reopen only its
        # materialized record for the later retry instead of changing the
        # logical graph or consuming another L3 attempt.
        if await self._handle_task_escalation(job):
            if job.status == JobStatus.WAITING_APPROVAL:
                records = plan.get("nodes") or {}
                for node in job.nodes:
                    logical_id = str((node.metadata or {}).get("logical_node_id") or node.id)
                    record = records.get(logical_id)
                    if isinstance(record, dict) and node.status == TaskStatus.PENDING:
                        record["status"] = "materialized"
                        record["error"] = ""
                        record["error_code"] = ""
                job.routing = dict(job.routing or {})
                job.routing["logical_plan"] = {
                    **pointer,
                    "revision": plan.get("revision", 1),
                    "progress": logical_plan_progress(plan),
                }
                await save_logical_plan(job.user_id, plan)
                await self._store.save_job(job)
            return True

        failed_nodes = [
            node for node in job.nodes
            if node.status in {TaskStatus.FAILED, TaskStatus.ESCALATED, TaskStatus.SKIPPED}
        ]
        if not failed_nodes:
            return False
        if not settings.AGENT_DYNAMIC_SUBGRAPH_ENABLED:
            job.routing = {**(job.routing or {}), "automatic_replan_blocked": "disabled"}
            await self._store.save_job(job)
            return False

        replan_count = int((job.routing or {}).get("replan_count") or 0)
        if replan_count >= settings.AGENT_SUBGRAPH_MAX_REPLANS:
            job.routing = {**(job.routing or {}), "automatic_replan_blocked": "replan_limit"}
            await self._store.save_job(job)
            return False

        # Never replace a suffix across a committed or uncertain external
        # effect.  The effect remains real even though its result may no
        # longer be in the small Job execution window.
        records = plan.get("nodes") or {}
        committed_effect = any(
            str(record.get("status") or "") == TaskStatus.COMPLETED.value
            and str(record.get("effect_status") or "") in {"committed", "uncertain"}
            for record in records.values()
            if isinstance(record, dict)
        )
        current_effect = any(
            node.effect_status in {"committed", "uncertain"}
            for node in job.nodes
        )
        if committed_effect or current_effect:
            job.routing = {**(job.routing or {}), "automatic_replan_blocked": "effectful_task"}
            await self._store.save_job(job)
            return False

        context = self._job_plan_context.get(job.job_id)
        planner_method = getattr(self._planner, "plan_for_level", None)
        if not context:
            job.routing = {**(job.routing or {}), "automatic_replan_blocked": "context_unavailable"}
            await self._store.save_job(job)
            return False
        if not callable(planner_method):
            job.routing = {**(job.routing or {}), "automatic_replan_blocked": "planner_not_level_aware"}
            await self._store.save_job(job)
            return False

        def public_text(value: object, limit: int) -> str:
            text = str(value or "").strip()
            try:
                from app.core.agent_security import redact_server_text

                text = redact_server_text(text)
            except Exception:  # noqa: BLE001
                pass
            return text[:limit]

        completed_evidence: list[dict] = []
        failed_evidence: list[dict] = []
        # Prefix bodies are read only briefly for the planner prompt.  They
        # remain result-store records and are never copied into Job.routing.
        from app.agents.orchestration.execution_lineage import resolve_result_ref

        for logical_id in list(plan.get("order") or []):
            record = records.get(logical_id)
            if not isinstance(record, dict):
                continue
            raw_node = record.get("node") or {}
            status = str(record.get("status") or "pending")
            if status == TaskStatus.COMPLETED.value:
                result = await resolve_result_ref(job.user_id, record.get("result_ref"))
                completed_evidence.append(
                    {
                        "step": public_text(raw_node.get("name") or logical_id, 120),
                        "result": public_text((result or {}).get("content") or (result or {}).get("output"), 900),
                    }
                )
            elif status in {TaskStatus.FAILED.value, TaskStatus.ESCALATED.value, TaskStatus.SKIPPED.value}:
                failed_evidence.append(
                    {
                        "step": public_text(raw_node.get("name") or logical_id, 120),
                        "method": public_text(
                            (raw_node.get("params") or {}).get("preferred_tool") or raw_node.get("agent"), 120
                        ),
                        "error_code": public_text(record.get("error_code"), 80),
                        "error": public_text(record.get("error"), 500),
                    }
                )

        evolution_context = (context.get("prior_summaries") or "").strip()
        evolution_context += "\n\n[当前任务执行反馈]\n" + json.dumps(
            {
                "instruction": (
                    "这是同一任务的计划演进。保留已完成产物，只规划尚未完成目标；"
                    "不要重复已成功步骤；失败方法不得原样重试，应更换工具、参数或实现原理。"
                ),
                "completed": completed_evidence[-20:],
                "failed": failed_evidence[-10:],
            },
            ensure_ascii=False,
            default=str,
        )
        tree = await planner_method(
            ComplexityLevel.M3,
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
            job.routing = {
                **(job.routing or {}),
                "replan_error": tree.error or tree.clarification or "未生成可执行替代步骤",
            }
            await self._store.save_job(job)
            return False

        self._prefer_atomic_steps(tree.nodes, job.request)
        self._adapt_unavailable_manifest_workers(tree.nodes)
        self._serialize_steps(tree.nodes)
        from app.agents.orchestration.dag import validate_planned_dag
        from app.agents.orchestration.presentation import attach_display_plan
        from app.agents.orchestration.safety import prepare_node_safety

        next_revision = int(plan.get("revision") or 1) + 1
        for node in tree.nodes:
            node.metadata = {**(node.metadata or {}), "plan_revision": next_revision}
            attach_display_plan(node)
            prepare_node_safety(node, job.user_id, job.job_id)
        errors = validate_planned_dag(tree.nodes, self._workers)
        if errors:
            job.routing = {**(job.routing or {}), "replan_error": "；".join(errors)[:500]}
            await self._store.save_job(job)
            return False

        failed_names = [item["step"] for item in failed_evidence if item.get("step")]
        reason = (
            f"原计划中的“{'、'.join(failed_names[:2])}”未能完成，已根据执行结果更换方法。"
            if failed_names else "原计划未能完成，已根据执行结果更换方法。"
        )
        replace_unfinished_tail(plan, tree.nodes, reason=reason)
        frontier = materialize_frontier(plan)
        if not frontier:
            job.status = JobStatus.FAILED
            job.error = "替代计划没有可执行前沿，已停止以避免重复执行。"
            job.routing = {**(job.routing or {}), "replan_error": "replacement_frontier_empty"}
            await save_logical_plan(job.user_id, plan)
            await self._store.save_job(job)
            return False

        plan_history = list((job.routing or {}).get("plan_history") or [])
        plan_history.append(
            {
                "revision": int(pointer.get("revision") or 1),
                "reason": reason,
                "changed_at": time.time(),
            }
        )
        job.nodes = frontier
        job.plan_text = tree.plan_text
        job.status = JobStatus.RUNNING
        job.error = None
        job.result = None
        job.updated_at = time.time()
        job.routing = {
            **(job.routing or {}),
            "level": ComplexityLevel.M3.value,
            "mode": "react",
            "replan_count": replan_count + 1,
            "plan_revision": plan.get("revision"),
            "plan_history": plan_history[-3:],
            "plan_change_reason": reason,
            "logical_plan": {
                **pointer,
                "revision": plan.get("revision"),
                "frontier_size": len(frontier),
                "progress": logical_plan_progress(plan),
                "used_estimated_tokens": (plan.get("budget") or {}).get("used_estimated", 0),
            },
        }
        await save_logical_plan(job.user_id, plan)
        await self._store.save_job(job)
        try:
            from app.core.observability import inc_agent_replan

            inc_agent_replan("logical", ComplexityLevel.M3.value, "logical_plan_failure")
        except Exception:  # noqa: BLE001
            pass
        logger.info("逻辑计划失败后挂载替代尾部: job={} revision={}", job.job_id[:8], plan.get("revision"))
        return True

    async def _maybe_replan_failed_job(self, job: Job, llm_api_key: str | None) -> bool:
        """Validate a result and evolve the visible plan with bounded retries.

        Replanning receives completed evidence and the concrete failed method.  This turns
        recovery into a stateful continuation instead of asking the planner to solve the
        original request again with no knowledge of what just happened.
        """
        # A rolling manifest owns its own terminal semantics: it records
        # per-item failures in the persisted audit and deliberately converges
        # after all entries settle.  Passing it into generic TCA escalation
        # would both overwrite that result and try to parse ``level=manifest``
        # as a ComplexityLevel.
        if isinstance(job.routing, dict) and isinstance(job.routing.get("manifest"), dict):
            return False
        if isinstance(job.routing, dict) and isinstance(job.routing.get("logical_plan"), dict):
            return await self._maybe_replan_logical_plan(job, llm_api_key)
        if job.scene != "office" or job.status in {
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
            JobStatus.PAUSED,
        }:
            return False
        # L2 is resolved by a pre-built control action; it does not invoke the
        # Planner nor allow a worker to mutate graph topology.  L3 falls
        # through to the bounded replacement-subgraph path below.
        if await self._handle_task_escalation(job):
            return True
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
            target = ComplexityLevel.M3 if replan_count < settings.AGENT_SUBGRAPH_MAX_REPLANS else None
        elif current == ComplexityLevel.M0 and target == ComplexityLevel.M1:
            # Escalate to a genuinely different planning method instead of
            # wrapping the failed deterministic primitive in another rule.
            target = ComplexityLevel.M2
        if (
            target is None
            or upgrade_count >= settings.AGENT_SUBGRAPH_MAX_REPLANS
            or replan_count >= settings.AGENT_SUBGRAPH_MAX_REPLANS
        ):
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
            elif node.status in {TaskStatus.FAILED, TaskStatus.ESCALATED}:
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
        from app.agents.orchestration.dag import validate_planned_dag
        from app.agents.orchestration.presentation import attach_display_plan
        from app.agents.orchestration.safety import prepare_node_safety

        current_revision = int(job.routing.get("plan_revision") or 1)
        next_revision = current_revision + 1
        completed_ids = [node.id for node in job.nodes if node.status == TaskStatus.COMPLETED]
        original_ids = {node.id for node in job.nodes}
        for node in tree.nodes:
            # The planner returns a proposal, never an in-place edit.  Prevent
            # accidental id collisions and attach only root nodes to durable,
            # completed evidence from the old graph.
            if node.id in original_ids:
                node.id = f"replan-{next_revision}-{uuid.uuid4().hex[:8]}"
            if not node.depends_on:
                node.depends_on = list(completed_ids)
            node.metadata = {**(node.metadata or {}), "plan_revision": next_revision}
            attach_display_plan(node)
            prepare_node_safety(node, job.user_id, job.job_id)
        # Validate the mounted graph as one graph. Root dependencies may point
        # at preserved completed anchors, which intentionally are not members
        # of the planner's proposal alone.
        anchor_nodes = [node for node in job.nodes if node.status == TaskStatus.COMPLETED]
        errors = validate_planned_dag([*anchor_nodes, *tree.nodes], self._workers)
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
        retired: list[str] = []
        preserved_nodes = []
        for old_node in job.nodes:
            if old_node.status == TaskStatus.COMPLETED:
                old_node.metadata = {**(old_node.metadata or {}), "plan_revision": next_revision}
                preserved_nodes.append(old_node)
                continue
            if old_node.status not in {TaskStatus.CANCELLED, TaskStatus.INTERRUPTED, TaskStatus.SKIPPED}:
                old_node.status = TaskStatus.SKIPPED
                old_node.error = "已由编排器生成的替代子图接管"
                old_node.completed_at = time.time()
            retired.append(old_node.id)
        mounted = list(job.routing.get("mounted_subgraphs") or [])
        mounted.append({
            "revision": next_revision,
            "anchor_nodes": completed_ids,
            "retired_nodes": retired,
            "node_ids": [node.id for node in tree.nodes],
            "reason": outcome.category.value,
        })
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
                "mounted_subgraphs": mounted[-6:],
            }
        )
        job.nodes = [*preserved_nodes, *tree.nodes]
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

    async def _handle_task_escalation(self, job: Job) -> bool:
        """Resolve L2 signals through deterministic orchestration controls.

        Missing prerequisites become a stable clarification result. Confirmation
        signals make one existing node wait for the API approval flow.  Neither
        branch permits an arbitrary new edge/node supplied by a worker.
        """
        from app.agents.orchestration.escalation import EscalationLevel, EscalationReason, coerce_escalation

        escalated = next((node for node in job.nodes if node.status == TaskStatus.ESCALATED), None)
        if escalated is None:
            return False
        signal = coerce_escalation((escalated.metadata or {}).get("escalation"), default_node_id=escalated.id)
        if signal is None or signal.level != EscalationLevel.TASK:
            return False
        job.routing = dict(job.routing or {})
        events = list(job.routing.get("escalations") or [])
        events.append({
            "level": signal.level.value,
            "reason": signal.reason.value,
            "node_id": escalated.id,
            "at": time.time(),
        })
        job.routing["escalations"] = events[-12:]
        if signal.reason == EscalationReason.APPROVAL_REQUIRED:
            tool = str((escalated.result or {}).get("tool") or (escalated.params or {}).get("preferred_tool") or "")
            tool_metadata = (escalated.result or {}).get("tool_metadata")
            if not isinstance(tool_metadata, dict):
                tool_metadata = {}
            approval_fingerprint = str(
                (escalated.result or {}).get("approval_fingerprint")
                or tool_metadata.get("approval_fingerprint")
                or ""
            )
            if not tool or not approval_fingerprint:
                job.status = JobStatus.FAILED
                job.error = "高风险步骤未提供完整的工具与参数审批标识，已停止执行。"
            else:
                metadata = dict(escalated.metadata or {})
                metadata["awaiting_approval"] = True
                metadata["approval_tool"] = tool
                metadata["approval_fingerprint"] = approval_fingerprint
                metadata["escalation"] = signal.model_dump(mode="json")
                escalated.metadata = metadata
                escalated.status = TaskStatus.PENDING
                escalated.error = None
                escalated.error_code = None
                job.status = JobStatus.WAITING_APPROVAL
                job.error = signal.message or "该步骤需要你的确认后才能继续。"
            job.updated_at = time.time()
            await self._store.save_job(job)
            return True
        if signal.reason in {
            EscalationReason.MISSING_PREREQUISITE,
            EscalationReason.PRECONDITION_FALSE,
        } or signal.requires_user_input:
            job.status = JobStatus.COMPLETED
            job.error = None
            job.result = {
                "type": "clarification",
                "question": signal.message or "完成该任务还需要补充必要信息。",
                "escalation": signal.model_dump(mode="json"),
            }
            job.updated_at = time.time()
            await self._store.save_job(job)
            return True
        return False

    # ── 查询 ────────────────────────────────────────────────

    async def get_job(self, job_id: str) -> Job | None:
        """Redis is authoritative for manifest runtime; static Temporal is frozen."""
        job = await self._store.get_job(job_id)
        # Only historical static workflows use the old workflow query. New
        # manifest workflows intentionally expose no full Job through history.
        if job is not None and str((job.routing or {}).get("runtime") or "") == "temporal_static" and await self._probe_temporal():
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
        await self._record_office_task_index(job)
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
            await self._stop_admission_heartbeat(job.job_id)
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
        stored_job = await self._store.get_job(job_id)
        if self._is_manifest_temporal_job(stored_job) and await self._probe_temporal():
            try:
                from app.agents.orchestration.temporal.client import (
                    signal_manifest_workflow,
                )
                if stored_job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
                    return stored_job
                await signal_manifest_workflow(job_id, "cancel_request", keep_completed)
                stored_job.status = JobStatus.CANCELLED
                stored_job.updated_at = time.time()
                await self._store.save_job(stored_job)
                await job_admission.release(job_id=stored_job.job_id, user_id=stored_job.user_id)
                await self._stop_admission_heartbeat(stored_job.job_id)
                return stored_job
            except Exception as exc:  # noqa: BLE001
                logger.warning("Temporal 清单取消任务失败，回退快照 {}: {}", job_id, exc)

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
        await self._stop_admission_heartbeat(job.job_id)
        return job

    async def approve_job(self, job_id: str, node_id: str, approved: bool = True) -> None:
        """Resolve an orchestrator-owned approval gate in either runtime."""
        job = await self._store.get_job(job_id)
        if job is None:
            raise RuntimeError("任务不存在")
        node = next((item for item in job.nodes if item.id == node_id), None)
        if node is None or not (node.metadata or {}).get("awaiting_approval"):
            raise RuntimeError("该任务节点当前不在等待审批")
        if not approved:
            node.status = TaskStatus.SKIPPED
            node.error = "用户拒绝审批"
            node.completed_at = time.time()
            job.status = JobStatus.FAILED
            job.error = "用户拒绝了高风险步骤的执行"
        else:
            metadata = dict(node.metadata or {})
            tool = str(metadata.get("approval_tool") or "")
            fingerprint = str(metadata.get("approval_fingerprint") or "")
            if not tool or not fingerprint:
                raise RuntimeError("审批凭证缺少工具参数绑定，不能安全恢复该步骤")
            metadata.pop("awaiting_approval", None)
            metadata["confirmed_tools"] = sorted({
                *(str(value) for value in (metadata.get("confirmed_tools") or [])), tool,
            } - {""})
            metadata["confirmed_tool_calls"] = sorted({
                *(str(value) for value in (metadata.get("confirmed_tool_calls") or [])), fingerprint,
            } - {""})
            node.metadata = metadata
            node.status = TaskStatus.PENDING
            node.error = None
            node.error_code = None
            job.status = JobStatus.RUNNING
            job.error = None
        job.updated_at = time.time()
        await self._store.save_job(job)
        if approved:
            self._live_jobs[job_id] = job
            if job_id not in self._tasks or self._tasks[job_id].done():
                self._tasks[job_id] = asyncio.create_task(self._run_job(job_id))

    async def pause_job(self, job_id: str) -> Job | None:
        """暂停任务（不调度新节点；运行中的节点会执行完）."""
        stored_job = await self._store.get_job(job_id)
        if self._is_manifest_temporal_job(stored_job) and await self._probe_temporal():
            try:
                from app.agents.orchestration.temporal.client import signal_manifest_workflow

                if stored_job.status == JobStatus.RUNNING:
                    await signal_manifest_workflow(job_id, "pause")
                    stored_job.status = JobStatus.PAUSED
                    stored_job.updated_at = time.time()
                    await self._store.save_job(stored_job)
                return stored_job
            except Exception as exc:  # noqa: BLE001
                logger.warning("Temporal 清单暂停任务失败，回退快照 {}: {}", job_id, exc)

        job = await self._store.get_job(job_id)
        if job is None or job.status != JobStatus.RUNNING:
            return job
        job.status = JobStatus.PAUSED
        job.updated_at = time.time()
        await self._store.save_job(job)
        return job

    async def resume_job(self, job_id: str) -> Job | None:
        """恢复被暂停的任务."""
        stored_job = await self._store.get_job(job_id)
        if self._is_manifest_temporal_job(stored_job) and await self._probe_temporal():
            try:
                from app.agents.orchestration.temporal.client import signal_manifest_workflow

                if stored_job.status == JobStatus.PAUSED:
                    await signal_manifest_workflow(job_id, "resume")
                    stored_job.status = JobStatus.RUNNING
                    stored_job.updated_at = time.time()
                    await self._store.save_job(stored_job)
                return stored_job
            except Exception as exc:  # noqa: BLE001
                logger.warning("Temporal 清单恢复任务失败，回退快照 {}: {}", job_id, exc)

        job = await self._store.get_job(job_id)
        if job is None or job.status != JobStatus.PAUSED:
            return job
        job.status = JobStatus.RUNNING
        job.updated_at = time.time()
        await self._store.save_job(job)
        return job


# 全局单例（API 层使用）
orchestrator = AgentOrchestrator()
