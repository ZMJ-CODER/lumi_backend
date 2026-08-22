"""Skill 调用遥测：持久化聚合，且只给候选排序提供低风险的辅助信号。"""

from __future__ import annotations

import time
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.agents.skills.capability import ToolCapability
from app.core.config import settings
from app.core.database import async_session_factory
from app.models.db_models import SkillTelemetryDaily

_hint_cache: dict[tuple[str, str], tuple[float, dict[tuple[str, str], float]]] = {}


def _error_class(value: str | None) -> str:
    raw = str(value or "none").upper()
    # 保留可行动的错误大类，避免把任意远端错误文本变成高基数指标。
    return raw if raw in {
        "NONE", "MCP_TIMEOUT", "MCP_UNAVAILABLE", "MCP_EXEC_ERROR",
        "INVALID_ARGS", "SANDBOX_REQUIRED", "EXEC_ERROR", "FORBIDDEN",
        "CONCURRENCY_LIMIT", "DAILY_LIMIT", "QUOTA_UNAVAILABLE",
    } else "OTHER"


async def record_skill_outcome(
    capability: ToolCapability,
    scene: str,
    *,
    success: bool,
    error_code: str | None,
    duration_ms: int,
) -> None:
    """Upsert one daily aggregate bucket; telemetry never interrupts a call."""
    values = {
        "metric_date": date.today(),
        "skill_name": capability.name[:200],
        "skill_version": capability.version[:32],
        "scene": (scene or "unknown")[:32],
        "error_class": _error_class(error_code),
        "calls": 1,
        "successes": 1 if success else 0,
        "duration_ms_total": max(0, int(duration_ms)),
    }
    try:
        async with async_session_factory() as session:
            stmt = insert(SkillTelemetryDaily).values(**values).on_conflict_do_update(
                constraint="uq_skill_telemetry_daily_bucket",
                set_={
                    "calls": SkillTelemetryDaily.calls + 1,
                    "successes": SkillTelemetryDaily.successes + values["successes"],
                    "duration_ms_total": SkillTelemetryDaily.duration_ms_total + values["duration_ms_total"],
                },
            )
            await session.execute(stmt)
            await session.commit()
    except Exception:
        return
    _hint_cache.pop((scene or "unknown", "all"), None)


async def apply_success_rate_hints(capabilities: list[ToolCapability], scene: str) -> None:
    """Inject cached scores; tool selection never performs a telemetry DB read."""
    if not capabilities:
        return
    cache_key = (scene or "unknown", "all")
    cached = _hint_cache.get(cache_key)
    if not cached:
        return
    rates = cached[1]
    for capability in capabilities:
        rate = rates.get((capability.name, capability.version))
        if rate is None:
            continue
        capability.annotations = {**capability.annotations, "success_rate": rate, "success_rate_source": "telemetry"}


async def refresh_success_rate_hints(scene: str) -> None:
    """Refresh one scene cache from startup/maintenance, outside tool routing."""
    cache_key = (scene or "unknown", "all")
    cutoff = date.today() - timedelta(days=max(1, settings.SKILL_TELEMETRY_LOOKBACK_DAYS))
    try:
        async with async_session_factory() as session:
            rows = await session.execute(
                select(
                    SkillTelemetryDaily.skill_name,
                    SkillTelemetryDaily.skill_version,
                    func.sum(SkillTelemetryDaily.calls),
                    func.sum(SkillTelemetryDaily.successes),
                ).where(
                    SkillTelemetryDaily.scene == (scene or "unknown"),
                    SkillTelemetryDaily.metric_date >= cutoff,
                ).group_by(SkillTelemetryDaily.skill_name, SkillTelemetryDaily.skill_version)
            )
            rates = {
                (str(name), str(version)): float(successes or 0) / float(calls)
                for name, version, calls, successes in rows
                if int(calls or 0) >= max(1, settings.SKILL_TELEMETRY_MIN_SAMPLES)
            }
    except Exception:
        return
    _hint_cache[cache_key] = (time.monotonic(), rates)
