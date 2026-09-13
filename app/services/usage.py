"""LLM token 用量统计：原始记录（llm_usage）+ 每日聚合（daily_token_stats）.

- 每次 LLM 调用写入一条 llm_usage（单条 INSERT，开销极小）；
- Celery 每日把截止昨天的原始记录聚合进 daily_token_stats（用户×日期×用途×模型），
  避免对原始明细做高频统计查询；
- 聚合后的原始行删除，原始记录最多保留到次日。
"""

import uuid
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_factory
from app.models.db_models import DailyTokenStat, LLMUsage

# 用途分类（"每方面"）
CATEGORY_CHAT = "chat"
CATEGORY_TOOL_DECISION = "tool_decision"
CATEGORY_SKILL = "skill"
CATEGORY_CODE = "code"
CATEGORY_PLAN = "plan"
CATEGORY_REVIEW = "review"
CATEGORY_MEMORY_EXTRACT = "memory_extract"
CATEGORY_MEMORY_MERGE = "memory_merge"
CATEGORY_MEMORY_PROFILE = "memory_profile"
CATEGORY_SUMMARY = "summary"
CATEGORY_TITLE = "title"
CATEGORY_REWRITE = "rewrite"
CATEGORY_PRIVACY_CONFIRM = "privacy_confirm"

#: 每百万 token 的估算价格（USD）：``model 子串 → (输入, 输出)``。
#:
#: 只用于 **Prometheus 成本速率**（回答"降级后的单任务成本有没有变贵"），不是账单口径：
#: 有些模型没有公开单价，就按同档位近似；查不到就返回 0（宁可少算，也不编一个数）。
#: 键是**小写子串**匹配，因此 ``deepseek-v4-flash`` 命中 ``deepseek`` 这一档。
MODEL_PRICE_USD_PER_MTOK: tuple[tuple[str, tuple[float, float]], ...] = (
    ("deepseek-reasoner", (0.55, 2.19)),
    ("deepseek", (0.27, 1.10)),
    ("qwen-vl", (0.35, 1.05)),
    ("qwen-turbo", (0.05, 0.20)),
    ("qwen", (0.30, 0.90)),
    ("gpt-4o-mini", (0.15, 0.60)),
    ("gpt-4o", (2.50, 10.00)),
    ("claude", (3.00, 15.00)),
)


def estimate_cost_usd(*, model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """按内置价目估算一次调用的成本（USD）；未知模型返回 ``0.0``。

    返回 0 的语义是"没算出来"，不是"免费"——所以 Grafana 上的成本曲线只用于**相对对比**
    （``is_fallback`` 两条线谁更贵），不能当财务数据用。
    """
    name = str(model or "").strip().casefold()
    if not name:
        return 0.0
    for keyword, (input_price, output_price) in MODEL_PRICE_USD_PER_MTOK:
        if keyword in name:
            return round(
                (max(0, int(prompt_tokens or 0)) / 1_000_000.0) * float(input_price)
                + (max(0, int(completion_tokens or 0)) / 1_000_000.0) * float(output_price),
                8,
            )
    return 0.0


def record_usage_metrics(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    fallback_used: bool = False,
    fallback_from: str = "",
    fallback_to: str = "",
    success: bool = True,
    cost_usd: float = 0.0,
    scene: str = "",
    provider: str = "",
    result: str = "",
) -> None:
    """把一次用量折进 Prometheus（失败静默：指标绝不能影响主流程）。

    label 只透传**低基数**维度（scene/provider/model/fallback_*/result）；调用方不要
    往里塞 user_id / job_id / prompt——那会让时间序列数随用户数线性增长。
    """
    try:
        from app.core.observability import record_llm_usage_metrics

        record_llm_usage_metrics(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            fallback_used=fallback_used,
            fallback_from=fallback_from,
            fallback_to=fallback_to,
            success=success,
            cost_usd=cost_usd,
            scene=scene,
            provider=provider,
            result=result,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("LLM 指标记录失败（不影响主流程）: {}", str(exc)[:120])


async def record_usage(
    user_id: str | None,
    category: str,
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    *,
    model_role: str | None = None,
    model_profile: str | None = None,
    config_source: str | None = None,
    fallback_used: bool = False,
    duration_ms: int = 0,
    structured_ok: bool | None = None,
    tool_calls: int = 0,
    complexity: str | None = None,
) -> None:
    """记录一次 LLM 调用用量（直接 await，单条 INSERT）。

    除既有的 category/model/token 外，额外记录**模型角色遥测**（方案 §九.5）：
    ``model_role`` / ``model_profile`` / ``config_source`` / ``fallback_used`` /
    ``duration_ms`` / ``structured_ok`` / ``tool_calls`` / ``complexity``。
    这样可以直接对比"切 cheap 前后"的调用次数、token、失败率、回退率与总成本。

    新增列在任何写入失败（老库未迁移）时降级为**只记旧字段**，绝不因为遥测失败
    影响主流程。
    """
    prompt_tokens = int(prompt_tokens or 0)
    completion_tokens = int(completion_tokens or 0)
    # 降级成本监控（方案 §5）：无论数据库写入成功与否，Prometheus 都必须拿到这次用量
    # ——指标是"率"的计算来源，不能依赖 llm_usage 表（老库缺列时它会被降级跳过）。
    # label 取值全部来自**低基数**维度（scene=category、provider=config_source）；
    # user_id 只进数据库，绝不进 label。
    record_usage_metrics(
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        fallback_used=fallback_used,
        fallback_from=str(config_source or "") if fallback_used else "",
        fallback_to=model if fallback_used else "",
        success=structured_ok is not False,
        result=("ok" if structured_ok is not False else "structured_error"),
        scene=str(category or ""),
        provider=str(config_source or ""),
        cost_usd=estimate_cost_usd(
            model=model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        ),
    )
    if prompt_tokens + completion_tokens <= 0:
        return
    uid = None
    if user_id:
        try:
            uid = uuid.UUID(str(user_id))
        except (ValueError, TypeError):
            uid = None
    telemetry = {
        "model_role": (str(model_role or "")[:40] or None),
        "model_profile": (str(model_profile or "")[:20] or None),
        "config_source": (str(config_source or "")[:40] or None),
        "fallback_used": bool(fallback_used),
        "duration_ms": int(duration_ms or 0),
        "structured_ok": structured_ok,
        "tool_calls": int(tool_calls or 0),
        "complexity": (str(complexity or "")[:20] or None),
    }
    try:
        async with async_session_factory() as session:
            session.add(
                LLMUsage(
                    user_id=uid,
                    category=category or CATEGORY_CHAT,
                    model=(model or "")[:100],
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    **telemetry,
                )
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        # 用量统计属于可观测性：写入失败绝不能影响聊天/检索等主流程
        # （典型场景：llm_usage 表缺失时仅降级为不统计，而不是让整个请求失败）
        logger.warning("LLM 用量记录失败（不影响主流程）: {}", exc)
        if any(value not in (None, False, 0) for value in telemetry.values()):
            try:
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
            except Exception as inner:  # noqa: BLE001
                logger.debug("LLM 用量旧字段回退写入也失败: {}", inner)


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


__all__ = [
    "CATEGORY_CHAT",
    "CATEGORY_CODE",
    "CATEGORY_MEMORY_EXTRACT",
    "CATEGORY_MEMORY_MERGE",
    "CATEGORY_MEMORY_PROFILE",
    "CATEGORY_PLAN",
    "CATEGORY_PRIVACY_CONFIRM",
    "CATEGORY_REVIEW",
    "CATEGORY_REWRITE",
    "CATEGORY_SKILL",
    "CATEGORY_SUMMARY",
    "CATEGORY_TITLE",
    "CATEGORY_TOOL_DECISION",
    "MODEL_PRICE_USD_PER_MTOK",
    "aggregate_daily_stats",
    "estimate_cost_usd",
    "estimate_tokens",
    "record_usage",
    "record_usage_metrics",
]
