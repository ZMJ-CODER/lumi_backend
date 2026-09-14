"""多智能体协作 API —— 提交任务 / 查询状态 / 终止 / 暂停 / 恢复 / 单步执行."""

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger

from app.agents.orchestration.orchestrator import orchestrator
from app.agents.orchestration.orchestrator import (
    ActiveConversationJobError,
    AgentBackpressureError,
    UserJobLimitError,
)
from app.core.deps import require_auth
from app.platform.security.throttling import consume_route_limit
from app.core.exceptions import (
    AppException,
    BadRequestException,
    ConflictException,
    ForbiddenException,
    NotFoundException,
    RateLimitException,
)
from app.models.agent import (
    ApproveAgentJobRequest,
    AppendPlanPatchRequest,
    CancelAgentJobRequest,
    CreateAgentJobRequest,
    ForkAgentJobRequest,
    RESUME_ACTION_RUN_NEXT,
    ResumeAgentJobRequest,
)

router = APIRouter()


async def _get_owned_job(job_id: str, user_id: str):
    """Resolve ownership before any state-changing job operation."""
    job = await orchestrator.get_job(job_id)
    if not job or job.user_id != user_id:
        raise NotFoundException("任务不存在")
    return job


async def _checkpoint_view_fields(job_id: str) -> dict:
    """步骤检查点摘要（只放计数与最大版本；读不到返回空字典，绝不抛错）。

    方案 §3.2：JobRunView 继续作为前端恢复模型，但**不放完整检查点**——完整记录走
    ``GET /api/v1/agents/jobs/{id}/steps`` 分页读取。
    """
    try:
        from app.services.step_checkpoint import checkpoint_view_fields, load_checkpoints

        return checkpoint_view_fields(await load_checkpoints(str(job_id or "")))
    except Exception as exc:  # noqa: BLE001 - 摘要失败不能影响任务详情
        logger.debug("检查点摘要读取失败（降级）: {}", str(exc)[:120])
        return {}


@router.post("/plan-preview")
async def preview_agent_plan(
    request: Request,
    req: CreateAgentJobRequest,
    payload: dict = Depends(require_auth),
):
    """验证真实路由、规划和编译结果，不创建或执行办公任务。"""
    if req.workspace_id and req.conversation_id:
        from app.workspace import service as workspaces

        try:
            workspaces.bind_workspace_to_conversation(payload["sub"], req.workspace_id, req.conversation_id)
        except (LookupError, ValueError) as exc:
            raise BadRequestException(str(exc), error_code="WORKSPACE_BINDING_INVALID") from exc
    tree, routing = await orchestrator.preview_plan(
        user_id=payload["sub"],
        request=req.request,
        scene=req.scene,
        conversation_id=req.conversation_id,
        project_id=req.project_id,
        project_ids=req.project_ids,
        llm_api_key=request.headers.get("x-llm-api-key") or None,
        clarification_answer=req.clarification_answer,
        office_docs=req.office_docs,
        workspace_id=req.workspace_id,
        user_role=payload.get("role", "user"),
    )
    return {
        "code": 0,
        "data": {
            "preview": True,
            "plan_text": tree.plan_text,
            "clarification": tree.clarification,
            "error": tree.error,
            "error_code": tree.error_code,
            "routing": routing,
            "nodes": [node.model_dump(mode="json") for node in tree.nodes],
        },
    }


@router.post("/jobs")
async def create_agent_job(
    request: Request,
    req: CreateAgentJobRequest,
    payload: dict = Depends(require_auth),
):
    """提交多智能体协作任务（规划 + 后台执行），立即返回任务及任务树.

    BYOK：用户自备 API key 通过 X-LLM-API-KEY 头临时携带，
    仅任务执行期间保存在内存，任务结束即释放，不落库不写日志。
    """
    llm_api_key = request.headers.get("x-llm-api-key") or None
    if req.workspace_id and req.conversation_id:
        from app.workspace import service as workspaces

        try:
            workspaces.bind_workspace_to_conversation(payload["sub"], req.workspace_id, req.conversation_id)
        except (LookupError, ValueError) as exc:
            raise BadRequestException(str(exc), error_code="WORKSPACE_BINDING_INVALID") from exc
    rate = await consume_route_limit(request, payload, "office_submit")
    if not rate.allowed:
        return JSONResponse(
            status_code=429,
            content={
                "code": 429,
                "message": "办公任务提交过于频繁，请稍后重试或切换普通模式对话",
                "data": {"error_code": "OFFICE_SUBMIT_RATE_LIMIT", "retry_after": rate.retry_after},
            },
            headers={"Retry-After": str(rate.retry_after)},
        )
    try:
        job = await orchestrator.submit_job(
            user_id=payload["sub"],
            request=req.request,
            scene=req.scene,
            conversation_id=req.conversation_id,
            project_id=req.project_id,
            project_ids=req.project_ids,
            llm_api_key=llm_api_key,
            clarification_answer=req.clarification_answer,
            office_docs=req.office_docs,
            workspace_id=req.workspace_id,
            user_role=payload.get("role", "user"),
            execution_preference=req.execution_preference,
            timeout_seconds=req.timeout_seconds,
        )
    except ActiveConversationJobError as exc:
        raise ConflictException(str(exc), error_code="OFFICE_JOB_CONFLICT") from exc
    except UserJobLimitError as exc:
        raise RateLimitException(str(exc), error_code="OFFICE_JOB_LIMIT") from exc
    except AgentBackpressureError as exc:
        raise RateLimitException(str(exc), error_code="OFFICE_JOB_BACKPRESSURE") from exc
    return {"code": 0, "data": job.model_dump()}


@router.get("/jobs")
async def list_agent_jobs(
    limit: int = Query(default=20, ge=1, le=100),
    payload: dict = Depends(require_auth),
):
    """列出我的多智能体任务（按提交时间倒序）."""
    jobs = await orchestrator.list_jobs(payload["sub"], limit)
    # 过程日志（每任务最多 200 条）只走详情接口的 run_view.process_log：列表是
    # 唯一的多任务响应，带上它会按任务数放大轮询载荷，任务卡片也不需要它。
    return {
        "code": 0,
        "data": {"items": [j.model_dump(exclude={"process_log"}) for j in jobs]},
    }


@router.get("/jobs/{job_id}")
async def get_agent_job(job_id: str, payload: dict = Depends(require_auth)):
    """查询任务状态与任务树（前端任务面板数据源）."""
    job = await orchestrator.get_job(job_id)
    if not job:
        # 不把 Redis key、连接串或其他用户信息回传给客户端；日志只记录截断
        # ID，便于区分“状态丢失”和“任务属于另一账号”两类 404。
        logger.warning("查询办公任务不存在: job={} user={}", str(job_id)[:12], str(payload.get("sub", ""))[:12])
        raise NotFoundException("任务不存在")
    if job.user_id != payload["sub"]:
        logger.warning("查询办公任务归属不匹配: job={} owner={} requester={}", str(job_id)[:12], str(job.user_id)[:12], str(payload.get("sub", ""))[:12])
        raise NotFoundException("任务不存在")
    # 前端页面刷新后需恢复 计划/步骤/当前状态/按钮数据：在原有 Job 快照上
    # 附带 run_view（execution_mode/plan_revision/current_step_index/steps/
    # canonical status/dsml_pending/task_completed/task_failed）。
    from lumi_orch.run_view import run_view

    data = job.model_dump()
    # 能力解析快照的时效性：``capability_resolution`` 是**提交期**结论，而客户端
    # Provider 是异步注册/心跳的。刷新后再返回，避免"界面报缺少 workspace.read@1，
    # 而同一次任务的读取步骤已经成功"这种自相矛盾的展示（刷新失败保留旧快照）。
    try:
        from app.agents.orchestration.preflight.capability_preflight_service import (
            refresh_capability_resolution,
        )

        await refresh_capability_resolution(job.routing)
        data["routing"] = job.routing
    except Exception as exc:  # noqa: BLE001 - 刷新失败不能影响任务详情
        logger.debug("能力解析刷新失败（降级）: {}", str(exc)[:120])
    view = run_view(job)
    # 过程气泡恢复：持久化的过程条目 + 由当前 Job 状态现推导的条目合并（去重且
    # ≤200）。内核 run_view 形状不动，只在 app 层补 process_log 字段。
    from app.contracts.process_log import merge_job_process_log, process_log_payload

    view["process_log"] = process_log_payload(merge_job_process_log(job))
    # 过程日志归档（阶段 3，ARCHIVE_CONTENT_V2，默认关闭）：快照里给出**真实可读**的
    # 归档引用（更早日志已转存为产物，读取走 GET /api/v1/artifacts/{ref}/content）。
    from app.services.process_log_archive import archive_view_fields

    view.update(archive_view_fields(job))
    # 步骤检查点摘要（方案 §3.2：JobRunView 只放**计数**，正文/完整检查点走
    # GET /api/v1/agents/jobs/{id}/steps 分页）；读不到时留空，不影响详情返回。
    view.update(await _checkpoint_view_fields(job.job_id))
    # 产物与声明式视图（刷新恢复 Artifact 卡片 / View 容器）：只放引用与元数据，
    # 下载仍走受权限保护的 /api/v1/artifacts/{artifact_id}/download。
    try:
        from app.services import artifacts as artifact_service
        from app.services import views as view_service

        refs = artifact_service.validate_artifacts(
            payload["sub"], artifact_service.artifacts_for_job(payload["sub"], job)
        )
        view["artifact_refs"] = refs
        job_views = view_service.views_for_job(payload["sub"], job, artifacts=refs)
        view["views"] = job_views
        # 前端既有渲染入口读的是 run_view.view_contributions；两个键同源，
        # 快照投影去掉 action（语义不同名），见 app/services/views.snapshot_contributions。
        view["view_contributions"] = view_service.snapshot_contributions(job_views)
    except Exception as exc:  # noqa: BLE001 - 产物/视图恢复失败不能影响任务详情
        logger.debug("产物/视图恢复失败（降级）: {}", str(exc)[:120])
    # 事件水位：前端把自己的 lastSeq 与它比较，决定是否走增量补拉。
    try:
        from app.services import job_event_log

        frames = await job_event_log.read_frames(job.job_id, after_seq=0, limit=1000)
        view["last_seq"] = job_event_log.last_seq_of(frames)
    except Exception as exc:  # noqa: BLE001
        logger.debug("事件水位读取失败（降级）: {}", str(exc)[:120])
    # 工作区操作快照（统一 OperationResult）：operation_summary / changed_files /
    # approval_state / rollback_available。完整 Diff 或文件内容不在这里，
    # 需要时走受权限保护的接口按需获取。读取失败降级为"没有操作记录"。
    try:
        from app.services.operation_snapshots import operation_summary_view

        view["operation_summary"] = await operation_summary_view(job.job_id)
    except Exception as exc:  # noqa: BLE001 - 快照读取失败不能影响任务详情
        logger.debug("操作快照读取失败（降级）: {}", str(exc)[:120])
    data["run_view"] = view
    # 契约校验：形状漂移（未知状态/丢步骤/缺按钮状态）在这里暴露，但**不改动**
    # 返回给前端的形状。
    from app.contracts.run_view import run_view_problems

    problems = run_view_problems(data["run_view"], expected_job_id=job.job_id)
    if problems:
        logger.warning(
            "run_view 契约校验未通过: job={} problems={}",
            str(job_id)[:12],
            "；".join(problems[:6]),
        )
    return {"code": 0, "data": data}


@router.get("/jobs/{job_id}/spans")
async def get_agent_job_spans(
    job_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    payload: dict = Depends(require_auth),
):
    """Return redacted node lifecycle spans for diagnosis and branch comparison."""
    job = await _get_owned_job(job_id, payload["sub"])
    from app.agents.orchestration.execution.lineage import list_node_spans

    return {
        "code": 0,
        "data": {
            "execution_id": job.execution_id or job.job_id,
            "parent_execution_id": job.parent_execution_id,
            "root_execution_id": job.root_execution_id or job.execution_id or job.job_id,
            "spans": await list_node_spans(job.execution_id or job.job_id, limit),
        },
    }


@router.get("/jobs/{job_id}/steps")
async def list_agent_job_steps(
    job_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    payload: dict = Depends(require_auth),
):
    """分页步骤记录（方案 §7.2）：只返回检查点的**摘要与引用**，绝不含正文。

    * 分页：``offset`` / ``limit``，总数在 ``total``；
    * 每条只给 ``status`` / ``effect_status`` / ``error_code`` / ``result_ref``
      （``{"id","sha256"}`` 最小引用）与时间戳；正文按引用另行获取；
    * 过程日志归档（超过 200 条的部分）入口由 ``GET /jobs/{id}`` 的
      ``log_archive_ref`` 给出，本接口不重复下发。
    """
    job = await _get_owned_job(job_id, payload["sub"])
    from app.services.step_checkpoint import load_checkpoints, recovery_view

    checkpoints = await load_checkpoints(job.job_id)
    rows = sorted(
        checkpoints,
        key=lambda item: (int(getattr(item, "checkpoint_version", 0) or 0), str(item.step_id)),
    )
    window = rows[offset : offset + limit]
    report = recovery_view(rows)
    return {
        "code": 0,
        "data": {
            "job_id": job.job_id,
            "total": len(rows),
            "offset": offset,
            "limit": limit,
            "has_more": offset + limit < len(rows),
            "steps": [_step_row(item) for item in window],
            **report.as_dict(),
        },
    }


@router.get("/jobs/{job_id}/results/{result_id}")
async def get_agent_job_result(
    job_id: str,
    result_id: str,
    mode: str = Query(default="full", pattern="^(full|summary)$"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=0, ge=0, le=500),
    max_chars: int = Query(default=0, ge=0, le=200000),
    fields: str = Query(default=""),
    payload: dict = Depends(require_auth),
):
    """按权限解析 ``result_ref``（方案 §7.2）：**正文按需加载**，不是快照的一部分。

    参数与前端 ``resultRefs.js`` / 主进程 ``result:fetch`` **逐字对齐**：

    * ``mode``：``full``（默认，完整正文）| ``summary``（只读摘要，不返回正文）；
    * ``offset`` / ``limit``：列表字段分页（前端只传，后端执行）；
    * ``max_chars``：字符预算（超出的文本字段截断并标 ``truncated``）；
    * ``fields``：逗号分隔的字段白名单（只读指定字段）。

    错误语义（前端据此分派，且**绝不返回空成功**）：

    * 归属由**任务所有者**推导，跨用户一律 403 / 任务不可见 404；
    * 过期 → **410** + ``data.error_code=RESULT_REF_EXPIRED``（终局，不可重试）；
    * 不存在 → **404** + 统一错误体（与"接口未上线"的 ``{"detail": ...}`` 区分）；
    * 完整性失败 → 404 + ``RESULT_REF_INTEGRITY_FAILED``；
    * ``sha256`` / ``schema_version`` / ``degraded`` 一并回传，前端据此做完整性校验、
      版本化解析与降级展示。
    """
    job = await _get_owned_job(job_id, payload["sub"])
    from app.services.result_store import (
        RESULT_REF_EXPIRED,
        RESULT_REF_FORBIDDEN,
        owner_key,
        resolve_result_for_owner,
    )

    field_list = tuple(item.strip() for item in str(fields or "").split(",") if item.strip())
    body, error_code = await resolve_result_for_owner(
        owner_key_value=owner_key(job.user_id),
        result_id=result_id,
        max_chars=max_chars,
        # 前端用 offset/limit 表达分页；预算与分页都由服务端执行。
        page_size=limit,
        page_offset=offset,
        fields=field_list,
        summary_only=str(mode or "").strip().lower() == "summary",
    )
    if error_code == RESULT_REF_FORBIDDEN:
        raise ForbiddenException("无权读取该结果引用")
    if error_code:
        # 过期是**终局**错误（不可重试）：410 让前端与"结果不存在(404)"区分开。
        status = 410 if error_code == RESULT_REF_EXPIRED else 404
        message = _result_error_message(error_code)
        raise AppException(
            status_code=status,
            message=message,
            data={"error_code": error_code, "result_id": str(result_id), "message": message},
            error_code=error_code,
        )
    return {
        "code": 0,
        "data": {
            "job_id": job.job_id,
            "result_id": str(result_id),
            **(body or {}),
        },
    }


def _result_error_message(error_code: str) -> str:
    """结果引用不可用时的用户文案（与 ``UnifiedError`` 口径一致，不暴露内部细节）。"""
    from lumi_contracts import spec_for

    return str(spec_for(str(error_code)).safe_message or "结果引用不可用")


def _step_row(checkpoint) -> dict:
    """检查点 → 接口行（白名单字段；正文一律按引用获取）。"""
    from lumi_contracts.persistence.checkpoint import runtime_status_for

    state = getattr(checkpoint, "status", "")
    step_id = str(getattr(checkpoint, "step_id", "") or "")
    return {
        "step_id": step_id,
        # 服务端权威去重键：与快照/实时过程日志的步骤条目**同一个** id（``step:<id>``），
        # 前端把本接口拉到的步骤与过程日志合并时不会出现重复行。
        "entry_id": f"step:{step_id}" if step_id else "",
        "attempt": int(getattr(checkpoint, "attempt", 1) or 1),
        "tool_name": str(getattr(checkpoint, "tool_name", "") or ""),
        "step_type": str(getattr(checkpoint, "step_type", "") or ""),
        "status": str(getattr(state, "value", state) or ""),
        "runtime_status": runtime_status_for(state),
        "effect_type": str(getattr(checkpoint, "effect_type", "") or ""),
        "effect_status": str(getattr(checkpoint, "effect_status", "") or ""),
        "error_code": str(getattr(checkpoint, "error_code", "") or ""),
        # 展示摘要：与过程日志的 summary 同源文案（前端时间线/步骤卡片直接用），
        # 完整正文按 ``result_ref`` 走 GET /jobs/{id}/results/{result_id}。
        "display_summary": str(getattr(checkpoint, "output_summary", "") or "")[:2000],
        "output_summary": str(getattr(checkpoint, "output_summary", "") or "")[:2000],
        "result_ref": (
            dict(checkpoint.result_ref) if isinstance(getattr(checkpoint, "result_ref", None), dict) else None
        ),
        "artifact_refs": [dict(item) for item in (getattr(checkpoint, "artifact_refs", None) or []) if isinstance(item, dict)],
        "checkpoint_version": int(getattr(checkpoint, "checkpoint_version", 0) or 0),
        "started_at": float(getattr(checkpoint, "started_at", 0.0) or 0.0),
        "finished_at": float(getattr(checkpoint, "finished_at", 0.0) or 0.0),
    }


@router.get("/jobs/{job_id}/recovery")
async def get_agent_job_recovery(
    job_id: str,
    load_dependencies: bool = Query(default=False),
    payload: dict = Depends(require_auth),
):
    """恢复核对报告（方案 §5）：**只读**，不触发任何重排或重跑。

    前端/运维据此判断"这个任务能不能自动恢复、哪些步骤要等人工"。副作用在途（``pending``）
    且可核对的步骤会出现在 ``reconcile_step_ids``，必须先核对实际状态。
    """
    job = await _get_owned_job(job_id, payload["sub"])
    from app.agents.orchestration.recovery.job_recovery_service import JobRecoveryService

    report = await JobRecoveryService().plan_for_job(job, load_dependencies=load_dependencies)
    return {"code": 0, "data": report.as_dict()}


@router.post("/jobs/{job_id}/fork")
async def fork_agent_job(
    job_id: str,
    req: ForkAgentJobRequest,
    request: Request,
    payload: dict = Depends(require_auth),
):
    """Fork a completed execution at a safe node; the original remains immutable."""
    await _get_owned_job(job_id, payload["sub"])
    try:
        job = await orchestrator.fork_job(
            job_id,
            node_id=req.node_id,
            params=req.params,
            instruction=req.instruction,
            llm_api_key=request.headers.get("x-llm-api-key") or None,
        )
    except UserJobLimitError as exc:
        raise RateLimitException(str(exc), error_code="OFFICE_JOB_LIMIT") from exc
    except RuntimeError as exc:
        raise BadRequestException(str(exc), error_code="OFFICE_FORK_REJECTED") from exc
    return {"code": 0, "data": job.model_dump(), "message": "已创建新的执行分支"}


@router.get("/jobs/{job_id}/stream")
async def get_agent_job_stream(
    job_id: str,
    node_id: str = "",
    cursor: int = 0,
    payload: dict = Depends(require_auth),
):
    """代码生成流式增量（cursor 游标轮询）：前端拿到增量后直接写盘.

    消息格式：{type: start|chunk|end, ...}；start 触发客户端截断重写该文件，
    chunk 为文本增量，end 标记本次流结束（ok=false 时客户端回滚备份）。
    """
    from app.services import code_stream

    if not node_id:
        raise NotFoundException("缺少 node_id")
    job = await orchestrator.get_job(job_id)
    if not job or job.user_id != payload["sub"]:
        raise NotFoundException("任务不存在")
    chunks, new_cursor = await code_stream.read_stream(job_id, node_id, cursor)
    return {"code": 0, "data": {"chunks": chunks, "cursor": new_cursor}}


@router.post("/jobs/{job_id}/cancel")
async def cancel_agent_job(
    job_id: str,
    req: CancelAgentJobRequest,
    payload: dict = Depends(require_auth),
):
    """终止任务：立即停止调度，可选择保留已完成节点/步骤与暂存成果."""
    await _get_owned_job(job_id, payload["sub"])
    effective_keep = (
        req.keep_completed_steps
        if req.keep_completed_steps is not None
        else req.keep_completed
    )
    job = await orchestrator.cancel_job(job_id, effective_keep)
    if not job:
        raise NotFoundException("任务不存在")
    from lumi_orch.run_view import run_view

    data = job.model_dump()
    view = run_view(job, status_override="cancelled")
    # 取消也是一次可恢复快照：同样带上合并后的过程日志（形状与 GET 一致）。
    from app.contracts.process_log import merge_job_process_log, process_log_payload

    view["process_log"] = process_log_payload(merge_job_process_log(job))
    from app.services.process_log_archive import archive_view_fields

    view.update(archive_view_fields(job))
    data["run_view"] = view
    data["cancel_reason"] = req.reason
    data["keep_completed_steps"] = bool(effective_keep)
    return {"code": 0, "data": data, "message": "任务已终止"}


@router.post("/jobs/{job_id}/approve")
async def approve_agent_job(
    job_id: str,
    req: ApproveAgentJobRequest,
    payload: dict = Depends(require_auth),
):
    """人工审批：批准/拒绝高风险节点（Human-in-the-Loop）."""
    await _get_owned_job(job_id, payload["sub"])
    try:
        await orchestrator.approve_job(job_id, req.node_id, req.approved)
    except RuntimeError as exc:
        raise BadRequestException(str(exc)) from exc
    return {"code": 0, "message": "已提交审批"}


@router.post("/jobs/{job_id}/pause")
async def pause_agent_job(job_id: str, payload: dict = Depends(require_auth)):
    """暂停任务（不调度新节点；运行中的节点会执行完）."""
    await _get_owned_job(job_id, payload["sub"])
    job = await orchestrator.pause_job(job_id)
    if not job:
        raise NotFoundException("任务不存在")
    return {"code": 0, "data": job.model_dump(), "message": "任务已暂停"}


@router.post("/jobs/{job_id}/resume")
async def resume_agent_job(
    job_id: str,
    req: ResumeAgentJobRequest | None = None,
    payload: dict = Depends(require_auth),
):
    """恢复任务 / 单步执行。

    - action=resume（默认）：恢复被暂停的任务，返回 JSON；
    - action=run_next：step_confirm 计划优先任务逐步骤执行，本接口即为
      SSE 事件流（step_started / delta / step_completed+waiting_next /
      waiting_approval / task_completed / task_failed / error / view），
      执行完“下一步”后收敛；前端在此连接的同一个气泡上消费过程事件。
    """
    await _get_owned_job(job_id, payload["sub"])
    action = str((req.action if req is not None else "") or "resume").strip() or "resume"
    if action == RESUME_ACTION_RUN_NEXT:
        return _run_next_sse_response(
            job_id,
            expected_step_id=(req.expected_step_id if req else "") or "",
            plan_revision=(req.plan_revision if req else None),
            idempotency_key=(req.idempotency_key if req else "") or "",
        )
    job = await orchestrator.resume_job(job_id)
    if not job:
        raise NotFoundException("任务不存在")
    return {"code": 0, "data": job.model_dump(), "message": "任务已恢复"}


def _run_next_sse_response(job_id: str, *, expected_step_id: str, plan_revision: int | None, idempotency_key: str):
    """构造 run_next 的 SSE 响应（事件流见 orchestrator.stream_run_next）。"""
    from app.contracts.events import SseEventEncoder, encode_sse
    from app.services.job_event_log import FrameRecorder

    async def event_gen():
        # 每条流一个编码器：seq 单调递增，前端据此发现丢帧；未知事件不抛错。
        encoder = SseEventEncoder(job_id=job_id)
        # 断线续传：帧同时写进任务事件日志（GET /agents/jobs/{id}/events?after_seq=）。
        recorder = FrameRecorder()
        # 终态封印：本流出现终态帧后，迟到的内容类帧一律吞掉（方案 §6.2）。
        from app.services.job_event_seal import StreamSeal

        seal = StreamSeal()
        try:
            async for evt in orchestrator.stream_run_next(
                job_id=job_id,
                expected_step_id=expected_step_id,
                plan_revision=plan_revision,
                idempotency_key=idempotency_key,
            ):
                kept, _dropped = seal.filter(frame for frame, _line in encoder.encode_frames(evt))
                for _frame in kept:
                    if recorder.add(_frame):
                        await recorder.flush()
                    yield encode_sse(_frame)
        except Exception as exc:  # noqa: BLE001
            logger.warning("run_next SSE 中断 job={} err={}", str(job_id)[:12], str(exc)[:200])
            kept, _dropped = seal.filter(
                frame
                for frame, _line in encoder.encode_frames({
                    "type": "error",
                    "message": "单步执行流中断，请刷新任务状态后重试",
                    "status": 500,
                    "code": "RUN_NEXT_STREAM_INTERRUPTED",
                })
            )
            for _frame in kept:
                if recorder.add(_frame):
                    await recorder.flush()
                yield encode_sse(_frame)
        finally:
            try:
                await recorder.flush()
            except Exception:  # noqa: BLE001 - 日志失败不影响已交付的事件
                pass

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/jobs/{job_id}/resume")
async def resume_agent_job_state(
    job_id: str,
    after_seq: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=1000),
    payload: dict = Depends(require_auth),
):
    """断线恢复的**唯一推荐入口**：读快照 + 只补增量（方案 §6）。

    与 ``GET /jobs/{job_id}/events`` 的差别：这里**不做全量重放**。快照本身就是一次
    "事件水位上的检查点"（``JobRunView.last_seq``），因此恢复 = 一次快照读 + 一次有界
    增量读，是毫秒级的；``/events`` 是纯增量接口，客户端拿它做后续轮询。

    客户端流程：``SEQ_GAP`` → 调本接口 → 用 ``snapshot`` 覆盖本地视图 →
    从 ``baseline_seq`` 起追加 ``events`` → 之后按 ``retry_after_ms`` 决定是否立刻再拉。
    """
    await _get_owned_job(job_id, payload["sub"])
    from app.services.resume_snapshot import build_gap_recovery

    data = await build_gap_recovery(job_id, after_seq=after_seq, limit=limit)
    return {"code": 0, "data": data, "message": data.get("message") or "已返回恢复包"}


@router.get("/jobs/{job_id}/tool-window")
async def read_agent_job_tool_window(
    job_id: str,
    limit: int = Query(20, ge=1, le=50),
    payload: dict = Depends(require_auth),
):
    """读回本任务的**工具窗口四层诊断快照**（最新在前）。

    回答的是线上最难问的问题之一："模型这次到底拿到了哪些工具，少了哪个，在哪一层少的"。
    每一帧都是同一场景下 ``catalog``（系统知道）→ ``eligible``（允许用）→ ``ranked``
    （排序候选）→ ``final``（真正传给模型）的四层证据，外加逐层差集与三态可见性。

    前端排障面板读法：
    * ``layers.final`` 才是模型真正看到的集合，**别拿 ``catalog`` 当窗口**；
    * ``dropped_core`` 非空 → 强制核心工具被挤掉了，属于服务端缺陷，标红而不是提示用户；
    * ``dropped_by_layer`` 直接指出"候选是在排序层丢的、还是在截断层丢的"；
    * ``visibility`` 里 ``unavailable`` 表示能力本身不可用（客户端离线/租约过期），
      这与"被窗口截断"是两种完全不同的处置。

    只返回名字与状态，不含任何参数/schema/正文，因此可以安全展示与落审计。
    Redis 降级或任务无快照时返回空列表（``available=false``），不报错。
    """
    await _get_owned_job(job_id, payload["sub"])
    from app.services.resume_snapshot import TOOL_WINDOW_MAX_ENTRIES, read_tool_windows

    windows = await read_tool_windows(job_id, limit=min(int(limit), TOOL_WINDOW_MAX_ENTRIES))
    dropped_core_total = sum(len(item.get("dropped_core") or []) for item in windows)
    return {
        "code": 0,
        "data": {
            "job_id": job_id,
            "available": bool(windows),
            "count": len(windows),
            "max_entries": TOOL_WINDOW_MAX_ENTRIES,
            "dropped_core_total": dropped_core_total,
            # 只要出现过核心工具被丢弃，就是服务端缺陷（不是用户可自行处理的情况）。
            "has_dropped_core": dropped_core_total > 0,
            "windows": windows,
        },
        "message": (
            "已返回工具窗口诊断快照"
            if windows
            else "暂无工具窗口诊断快照（未开启工具注册表派生，或诊断数据已过期）"
        ),
    }


@router.get("/jobs/{job_id}/events")
async def replay_agent_job_events(
    job_id: str,
    after_seq: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=1000),
    payload: dict = Depends(require_auth),
):
    """按 ``seq`` 补拉任务事件（断线续传）。

    与实时流**同一份标准帧**：同一个 ``event_id`` / ``seq`` / ``type`` / ``payload``，
    因此前端可以"先 ``JSON.parse`` 增量，再按 ``event_id`` 去重"，不会重复渲染。

    * ``after_seq``：只返回 ``seq`` 严格大于该值的事件（前端传自己的 ``lastSeq``）；
    * ``limit``：单次上限（默认 500，最大 1000）；返回体里的 ``last_seq`` 可直接
      写回前端的 ``lastSeq``；
    * 事件日志不可用（Redis 降级/过期）时返回空列表 + ``truncated=true``，
      此时应以 ``GET /agents/jobs/{job_id}`` 的 ``JobRunView`` 快照恢复为准。
    """
    await _get_owned_job(job_id, payload["sub"])
    from app.contracts.events import STREAM_EVENT_VERSION, SseEventEncoder
    from app.services import job_event_log

    frames, log_state = await job_event_log.read_frames_with_state(job_id, after_seq=after_seq, limit=limit)
    # 快照水位：告诉客户端"从哪个 seq 起用增量就够"。快照已经覆盖的事件不必再发，
    # 也不该让客户端以为"没收到事件 = 状态丢了"（方案 §6 的 Snapshot 真空期）。
    snapshot_seq = 0
    try:
        from app.services.job_snapshot_store import read_snapshot

        view = await read_snapshot(job_id)
        snapshot_seq = int(getattr(view, "last_seq", 0) or 0) if view is not None else 0
    except Exception:  # noqa: BLE001 - 快照不可用不影响增量补拉
        snapshot_seq = 0
    log_available = bool(log_state.get("available"))
    head_seq = int(log_state.get("head_seq") or 0)
    oldest_seq = int(log_state.get("oldest_seq") or 0)
    # 协议与版本必须**如实回报**（补拉里存的就是实时流同一份帧）：
    # 双协议期后端可能仍是 legacy 帧，不能一律写 canonical；版本也不能写死
    # （前端对 version > 支持版本 的帧会走 UNSUPPORTED_VERSION 降级）。
    from lumi_contracts import EVENT_ENVELOPE_VERSION

    if frames:
        protocol = "canonical" if any(isinstance(item.get("payload"), dict) for item in frames) else "legacy"
    else:
        protocol = SseEventEncoder().protocol
    version = EVENT_ENVELOPE_VERSION if protocol == "canonical" else STREAM_EVENT_VERSION
    last_returned = job_event_log.last_seq_of(frames) or int(after_seq)
    return {
        "code": 0,
        "data": {
            "job_id": job_id,
            "protocol": protocol,
            "version": int(version),
            "after_seq": int(after_seq),
            "last_seq": last_returned,
            "count": len(frames),
            "truncated": len(frames) >= limit,
            # 快照水位与日志水位：客户端据此判断"要不要再拉"以及"本地视图落后多少"。
            # 只有 last_seq 的话，客户端无法区分"追平了"与"日志被我自己的水位过滤光了"。
            "snapshot_seq": int(snapshot_seq),
            "head_seq": int(head_seq),
            # 事件日志可读性：False 时"没有事件"没有结论意义（读失败也是空数组）。
            "event_log_available": log_available,
            "oldest_seq": int(oldest_seq),
            # 明确缺口：日志起点已经晚于客户端水位的下一条（中间被裁剪）。
            "gap_detected": bool(log_available and oldest_seq and oldest_seq > int(after_seq) + 1),
            "caught_up": bool(log_available and int(head_seq) <= int(last_returned)),
            "events": frames,
        },
        "message": (
            "已返回增量事件"
            if frames
            else ("没有新的增量事件（可用快照恢复）" if log_available else "事件日志暂不可读，无法确认是否已追平")
        ),
    }


@router.post("/jobs/{job_id}/plan-patches")
async def append_agent_plan_patch(
    job_id: str,
    req: AppendPlanPatchRequest,
    payload: dict = Depends(require_auth),
):
    """向已就绪的计划插槽追加通过校验的运行期节点。"""
    await _get_owned_job(job_id, payload["sub"])
    try:
        outcome = await orchestrator.append_plan_patch(
            job_id,
            payload["sub"],
            req.as_external_patch(),
        )
    except Exception as exc:  # noqa: BLE001
        from lumi_orch import PlanPatchConflict

        if isinstance(exc, PlanPatchConflict):
            raise ConflictException(str(exc), error_code="PLAN_PATCH_CONFLICT") from exc
        raise
    return {
        "code": 0,
        "data": {
            "job": outcome.job.model_dump(),
            "patch": {
                "patch_id": outcome.patch_id,
                "slot_id": outcome.slot_id,
                "revision": outcome.revision,
                "replayed": outcome.replayed,
                "temporal_signaled": outcome.temporal_signaled,
            },
        },
    }
