"""多智能体协作 API —— 提交任务 / 查询状态 / 终止 / 暂停 / 恢复."""

from fastapi import APIRouter, Depends, Query, Request

from app.agents.orchestration import orchestrator
from app.core.deps import require_auth
from app.core.exceptions import NotFoundException
from app.models.agent import ApproveAgentJobRequest, CancelAgentJobRequest, CreateAgentJobRequest

router = APIRouter()


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
    job = await orchestrator.submit_job(
        payload["sub"],
        req.request,
        req.scene,
        req.project_id,
        req.project_ids,
        llm_api_key,
        req.clarification_answer,
        req.office_docs,
    )
    return {"code": 0, "data": job.model_dump()}


@router.get("/jobs")
async def list_agent_jobs(
    limit: int = Query(default=20, ge=1, le=100),
    payload: dict = Depends(require_auth),
):
    """列出我的多智能体任务（按提交时间倒序）."""
    jobs = await orchestrator.list_jobs(payload["sub"], limit)
    return {"code": 0, "data": {"items": [j.model_dump() for j in jobs]}}


@router.get("/jobs/{job_id}")
async def get_agent_job(job_id: str, payload: dict = Depends(require_auth)):
    """查询任务状态与任务树（前端任务面板数据源）."""
    job = await orchestrator.get_job(job_id)
    if not job or job.user_id != payload["sub"]:
        raise NotFoundException("任务不存在")
    return {"code": 0, "data": job.model_dump()}


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
    """终止任务：立即停止调度，可选择保留已完成节点."""
    job = await orchestrator.cancel_job(job_id, req.keep_completed)
    if not job or job.user_id != payload["sub"]:
        raise NotFoundException("任务不存在")
    return {"code": 0, "data": job.model_dump(), "message": "任务已终止"}


@router.post("/jobs/{job_id}/approve")
async def approve_agent_job(
    job_id: str,
    req: ApproveAgentJobRequest,
    payload: dict = Depends(require_auth),
):
    """人工审批：批准/拒绝高风险节点（Human-in-the-Loop）."""
    try:
        await orchestrator.approve_job(job_id, req.node_id, req.approved)
    except RuntimeError as exc:
        raise BadRequestException(str(exc)) from exc
    return {"code": 0, "message": "已提交审批"}


@router.post("/jobs/{job_id}/pause")
async def pause_agent_job(job_id: str, payload: dict = Depends(require_auth)):
    """暂停任务（不调度新节点；运行中的节点会执行完）."""
    job = await orchestrator.pause_job(job_id)
    if not job or job.user_id != payload["sub"]:
        raise NotFoundException("任务不存在")
    return {"code": 0, "data": job.model_dump(), "message": "任务已暂停"}


@router.post("/jobs/{job_id}/resume")
async def resume_agent_job(job_id: str, payload: dict = Depends(require_auth)):
    """恢复被暂停的任务."""
    job = await orchestrator.resume_job(job_id)
    if not job or job.user_id != payload["sub"]:
        raise NotFoundException("任务不存在")
    return {"code": 0, "data": job.model_dump(), "message": "任务已恢复"}
