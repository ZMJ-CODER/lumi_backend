"""Token 用量查询（用户侧）：今日汇总 + 按用途 + 累计."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_auth
from app.models.db_models import DailyTokenStat, LLMUsage

router = APIRouter()

_CATEGORY_LABELS = {
    "chat": "对话",
    "tool_decision": "工具决策",
    "skill": "技能",
    "code": "代码生成",
    "plan": "任务规划",
    "review": "审查",
    "memory_extract": "记忆抽取",
    "memory_merge": "记忆合并",
    "memory_profile": "画像",
    "summary": "摘要",
    "title": "步骤标题",
    "rewrite": "提问改写",
    "privacy_confirm": "隐私确认",
}


def _uid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def _row(row) -> dict:
    return {
        "prompt_tokens": int(row.p or 0),
        "completion_tokens": int(row.c or 0),
        "total_tokens": int((row.p or 0) + (row.c or 0)),
        "calls": int(row.n or 0),
    }


@router.get("")
async def get_my_usage(
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_auth),
):
    """当前用户 token 用量：今日汇总 + 按用途 + 累计（含已聚合的历史天）."""
    uid = _uid(payload["sub"])
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    today = _row(
        (
            await db.execute(
                select(
                    func.sum(LLMUsage.prompt_tokens).label("p"),
                    func.sum(LLMUsage.completion_tokens).label("c"),
                    func.count().label("n"),
                ).where(LLMUsage.user_id == uid, LLMUsage.created_at >= today_start)
            )
        ).one()
    )

    by_cat_rows = (
        await db.execute(
            select(
                LLMUsage.category,
                func.sum(LLMUsage.prompt_tokens).label("p"),
                func.sum(LLMUsage.completion_tokens).label("c"),
                func.count().label("n"),
            )
            .where(LLMUsage.user_id == uid, LLMUsage.created_at >= today_start)
            .group_by(LLMUsage.category)
            .order_by(func.sum(LLMUsage.prompt_tokens).desc())
        )
    ).all()

    # 累计 = 未聚合的 llm_usage + 已聚合的 daily_token_stats
    raw_total = (
        await db.execute(
            select(
                func.sum(LLMUsage.prompt_tokens).label("p"),
                func.sum(LLMUsage.completion_tokens).label("c"),
                func.count().label("n"),
            ).where(LLMUsage.user_id == uid)
        )
    ).one()
    agg_total = (
        await db.execute(
            select(
                func.sum(DailyTokenStat.prompt_tokens).label("p"),
                func.sum(DailyTokenStat.completion_tokens).label("c"),
                func.sum(DailyTokenStat.call_count).label("n"),
            ).where(DailyTokenStat.user_id == uid)
        )
    ).one()
    total = {
        "prompt_tokens": int(raw_total.p or 0) + int(agg_total.p or 0),
        "completion_tokens": int(raw_total.c or 0) + int(agg_total.c or 0),
        "total_tokens": int(raw_total.p or 0)
        + int(agg_total.p or 0)
        + int(raw_total.c or 0)
        + int(agg_total.c or 0),
        "calls": int(raw_total.n or 0) + int(agg_total.n or 0),
    }

    return {
        "code": 0,
        "data": {
            "today": today,
            "total": total,
            "by_category": [
                {
                    "category": r.category,
                    "label": _CATEGORY_LABELS.get(r.category, r.category),
                    **_row(r),
                }
                for r in by_cat_rows
            ],
        },
    }
