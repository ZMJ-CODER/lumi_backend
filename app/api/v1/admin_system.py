"""系统级管理后台 API —— 工程化运维数据（用量/任务/记忆/审计/维护任务）.

权限：全部 require_admin；用户管理/触发维护任务 require_superadmin。
知识库管理仍走 admin.py（知识空间/文档）。
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.orchestration import orchestrator
from app.core.database import get_db
from app.core.deps import require_admin, require_superadmin
from app.core.rag_config import get_rag_overrides
from app.models.db_models import (
    ControlLog,
    DailyTokenStat,
    Document,
    DocumentChunk,
    KnowledgeSpace,
    Memory,
    MemoryProfile,
    Message,
    Conversation,
    User,
)

router = APIRouter()

# 可触发的维护任务白名单（Celery）
MAINTENANCE_TASKS = {
    "build_all_user_profiles": "celery_app.tasks.build_all_user_profiles",
    "cleanup_memories": "celery_app.tasks.cleanup_memories",
    "cleanup_conversations": "celery_app.tasks.cleanup_conversations",
    "aggregate_token_stats": "celery_app.tasks.aggregate_token_stats",
    "cleanup_vectors": "celery_app.tasks.cleanup_vectors",
}


async def _count(db: AsyncSession, model) -> int:
    return (await db.execute(select(func.count()).select_from(model))).scalar_one()


@router.get("/overview")
async def system_overview(db: AsyncSession = Depends(get_db), payload: dict = Depends(require_admin)):
    """系统概览：数据量 + 基础设施状态."""
    from app.core.redis import get_redis

    counts = {
        "users": await _count(db, User),
        "conversations": await _count(db, Conversation),
        "messages": await _count(db, Message),
        "documents": await _count(db, Document),
        "chunks": await _count(db, DocumentChunk),
        "knowledge_spaces": await _count(db, KnowledgeSpace),
        "memories": await _count(db, Memory),
        "profiles": await _count(db, MemoryProfile),
        "control_logs": await _count(db, ControlLog),
    }
    status = {"database": "ok", "redis": "ok", "temporal": "unknown"}
    try:
        r = get_redis()
        await r.ping()
    except Exception:  # noqa: BLE001
        status["redis"] = "down"
    try:
        await orchestrator._probe_temporal()
        status["temporal"] = "ok" if orchestrator._temporal_available else "down"
    except Exception:  # noqa: BLE001
        status["temporal"] = "down"
    return {"code": 0, "data": {"counts": counts, "status": status}}


@router.get("/usage")
async def system_usage(
    days: int = Query(default=7, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_admin),
):
    """Token 用量：按日期聚合（daily_token_stats），可选按用户筛选."""
    since = date.today() - timedelta(days=days - 1)
    stmt = (
        select(
            DailyTokenStat.stat_date,
            func.sum(DailyTokenStat.prompt_tokens).label("prompt"),
            func.sum(DailyTokenStat.completion_tokens).label("completion"),
            func.sum(DailyTokenStat.call_count).label("calls"),
        )
        .where(DailyTokenStat.stat_date >= since)
        .group_by(DailyTokenStat.stat_date)
        .order_by(DailyTokenStat.stat_date)
    )
    rows = (await db.execute(stmt)).all()
    items = [
        {
            "date": str(r.stat_date),
            "prompt_tokens": int(r.prompt or 0),
            "completion_tokens": int(r.completion or 0),
            "calls": int(r.calls or 0),
        }
        for r in rows
    ]
    return {"code": 0, "data": {"items": items, "days": days}}


@router.get("/jobs")
async def system_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    payload: dict = Depends(require_admin),
):
    """跨用户任务列表（最近提交）."""
    jobs = await orchestrator.admin_list_jobs(limit)
    items = [
        {
            "job_id": j.job_id,
            "user_id": j.user_id,
            "request": (j.request or "")[:120],
            "scene": j.scene,
            "status": j.status.value if hasattr(j.status, "value") else str(j.status),
            "node_count": len(j.nodes or []),
            "agents": [n.agent for n in (j.nodes or [])][:5],
            "error": (j.error or "")[:200],
            "created_at": j.created_at,
            "updated_at": j.updated_at,
        }
        for j in jobs
    ]
    return {"code": 0, "data": {"items": items}}


@router.post("/jobs/{job_id}/cancel")
async def cancel_system_job(
    job_id: str,
    payload: dict = Depends(require_admin),
):
    """管理员终止任意任务（保留已完成节点）."""
    job = await orchestrator.cancel_job(job_id, keep_completed=True)
    if job is None:
        from app.core.exceptions import NotFoundException

        raise NotFoundException("任务不存在")
    return {"code": 0, "data": job.model_dump(), "message": "任务已终止"}


@router.get("/memories")
async def system_memories(db: AsyncSession = Depends(get_db), payload: dict = Depends(require_admin)):
    """记忆统计：总量 / 类型分布 / 隐私等级分布."""
    total = await _count(db, Memory)
    by_type = {
        str(r[0]): int(r[1])
        for r in (await db.execute(select(Memory.memory_type, func.count()).group_by(Memory.memory_type))).all()
    }
    by_privacy = {
        str(r[0]): int(r[1])
        for r in (await db.execute(select(Memory.privacy_level, func.count()).group_by(Memory.privacy_level))).all()
    }
    profiles = await _count(db, MemoryProfile)
    return {"code": 0, "data": {"total": total, "by_type": by_type, "by_privacy": by_privacy, "profiles": profiles}}


@router.post("/tasks/{task_name}")
async def trigger_maintenance_task(
    task_name: str,
    payload: dict = Depends(require_superadmin),
):
    """手动触发维护任务（Celery）：画像重建 / 记忆清理 / 会话裁剪 / token 聚合 / 向量清理."""
    from celery_app import celery_app

    task = MAINTENANCE_TASKS.get(task_name)
    if not task:
        from app.core.exceptions import BadRequestException

        raise BadRequestException(f"未知维护任务: {task_name}，可选 {list(MAINTENANCE_TASKS)}")
    celery_app.send_task(task)
    return {"code": 0, "message": f"已触发任务 {task_name}"}


@router.get("/audit")
async def system_audit(
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_admin),
):
    """审计日志：最近技能/工具调用（control_logs）."""
    stmt = (
        select(ControlLog, User.account)
        .join(User, User.id == ControlLog.user_id)
        .order_by(ControlLog.created_at.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    items = [
        {
            "id": str(c.id),
            "account": account,
            "action": c.action,
            "target": (c.target or "")[:300],
            "success": c.success,
            "detail": (c.detail or "")[:500],
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c, account in rows
    ]
    return {"code": 0, "data": {"items": items}}


@router.get("/rag-config")
async def system_rag_config(payload: dict = Depends(require_admin)):
    """当前 RAG 动态配置覆盖（Redis）."""
    return {"code": 0, "data": {"overrides": await get_rag_overrides()}}
