"""LLM token 用量统计：原始记录（llm_usage）+ 每日聚合（daily_token_stats）.

- 每次 LLM 调用写入一条 llm_usage（单条 INSERT，开销极小）；
- Celery 每日把截止昨天的原始记录聚合进 daily_token_stats（用户×日期×用途×模型），
  避免对原始明细做高频统计查询；
- 聚合后的原始行删除，原始记录最多保留到次日。
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_factory
from app.models.db_models import DailyTokenStat, LLMUsage

# 用途分类（"每方面"）
CATEGORY_CHAT = "chat"
CATEGORY_TOOL_DECISION = "tool_decision"
CATEGORY_MEMORY_EXTRACT = "memory_extract"
CATEGORY_MEMORY_MERGE = "memory_merge"
CATEGORY_MEMORY_PROFILE = "memory_profile"
CATEGORY_SUMMARY = "summary"
CATEGORY_TITLE = "title"
CATEGORY_REWRITE = "rewrite"
CATEGORY_PRIVACY_CONFIRM = "privacy_confirm"


async def record_usage(
    user_id: str | None,
    category: str,
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> None:
    """记录一次 LLM 调用用量（直接 await，单条 INSERT）."""
    prompt_tokens = int(prompt_tokens or 0)
    completion_tokens = int(completion_tokens or 0)
    if prompt_tokens + completion_tokens <= 0:
        return
    uid = None
    if user_id:
        try:
            uid = uuid.UUID(str(user_id))
        except (ValueError, TypeError):
            uid = None
    async with async_session_factory() as session:
        session.add(
            LLMUsage(
                user_id=uid,
                category=category or CATEGORY_CHAT,
                model=(model or "")[:100],
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        )
        await session.commit()


def estimate_tokens(text: str) -> int:
    """粗略估算 token：中文 1 字符≈1，其他 3 字符≈1（与编排器一致，用于流式无 usage 时兜底）."""
    if not text:
        return 0
    cjk = sum(
        1
        for ch in text
        if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f" or "\uff00" <= ch <= "\uffef"
    )
    return cjk + (len(text) - cjk) // 3 + 2


async def aggregate_daily_stats(session: AsyncSession) -> int:
    """把截止昨天的 llm_usage 聚合进 daily_token_stats 并删除原始行（幂等）."""
    today = datetime.now(timezone.utc).date()
    rows = (
        await session.execute(
            select(
                LLMUsage.user_id,
                func.date(LLMUsage.created_at).label("d"),
                LLMUsage.category,
                LLMUsage.model,
                func.sum(LLMUsage.prompt_tokens).label("p"),
                func.sum(LLMUsage.completion_tokens).label("c"),
                func.count().label("n"),
            )
            .where(func.date(LLMUsage.created_at) < today)
            .group_by(
                LLMUsage.user_id,
                func.date(LLMUsage.created_at),
                LLMUsage.category,
                LLMUsage.model,
            )
        )
    ).all()
    for r in rows:
        stat = (
            await session.execute(
                select(DailyTokenStat).where(
                    DailyTokenStat.user_id == r.user_id,
                    DailyTokenStat.stat_date == r.d,
                    DailyTokenStat.category == r.category,
                    DailyTokenStat.model == r.model,
                )
            )
        ).scalar_one_or_none()
        if stat:
            stat.prompt_tokens += r.p
            stat.completion_tokens += r.c
            stat.call_count += r.n
        else:
            session.add(
                DailyTokenStat(
                    user_id=r.user_id,
                    stat_date=r.d,
                    category=r.category,
                    model=r.model,
                    prompt_tokens=r.p,
                    completion_tokens=r.c,
                    call_count=r.n,
                )
            )
    await session.execute(delete(LLMUsage).where(func.date(LLMUsage.created_at) < today))
    await session.commit()
    return len(rows)
