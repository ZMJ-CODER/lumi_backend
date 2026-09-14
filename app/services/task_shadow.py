"""任务画像影子模式：新旧判定的**结构化差异记录**（方案 4 §5）。

先并行记录、后切换、最后删除：

* 影子期新旧判定同时对每个请求运行，本模块记录差异与耗时；
* **不改变实际路由**（``profile_authoritative=False``），只落观测数据；
* 记录与报告都只含枚举/原因码，不含用户原文与模型推理；
* 达标（写入相关差异收敛、超时率可接受）后才切换；切换后旧词表只留诊断兜底。

存放位置与过程日志/检查点同一套机制：Redis hash（有界 + TTL），不可用时回退进程内存；
**影子记录失败绝不影响任务执行**（只记日志）。
"""

from __future__ import annotations

import json
import time
from typing import Any

from loguru import logger

from lumi_contracts.routing.shadow import (
    DiscrepancyType,
    RouteSource,
    ShadowRecord,
    ShadowReport,
    build_shadow_report,
    classify_discrepancy,
)

#: 影子记录键前缀（与 Job 快照/检查点分离：影子数据不参与恢复）。
SHADOW_KEY_PREFIX = "task_shadow:"
#: 每个任务保留的最大记录数（有界：影子数据只用于统计，不用于恢复）。
MAX_RECORDS_PER_JOB = 200
#: 聚合统计键（跨任务计数，便于回答"差异率收敛了吗"）。
SHADOW_STATS_KEY = "task_shadow:stats"
_STATS_TTL_SECONDS = 7 * 24 * 3600


def shadow_key(job_id: str) -> str:
    return f"{SHADOW_KEY_PREFIX}{job_id}"


def shadow_enabled() -> bool:
    """影子模式是否打开（``INTEGRATION_SHADOW_MODE``，默认关闭）。"""
    try:
        from app.platform.runtime.feature_flags import shadow_mode

        return shadow_mode()
    except Exception:  # noqa: BLE001 - 开关不可用时按关闭处理（保守）
        return False


def build_record(
    *,
    trace_id: str = "",
    job_id: str = "",
    legacy_requires_orchestration: bool,
    profile_requires_orchestration: bool,
    legacy_route_mode: str = "",
    profile_route_mode: str = "",
    legacy_reason_code: str = "",
    profile_reason_code: str = "",
    confidence_source: str = "",
    assessor_ms: int = 0,
    assessor_timed_out: bool = False,
    profile_authoritative: bool = False,
    legacy_complex: bool | None = None,
    profile_complex: bool | None = None,
) -> ShadowRecord:
    """构造一条影子记录（差异分类复用契约的纯函数）。"""
    discrepancy = classify_discrepancy(
        legacy_requires_orchestration=bool(legacy_requires_orchestration),
        profile_requires_orchestration=bool(profile_requires_orchestration),
        legacy_complex=legacy_complex,
        profile_complex=profile_complex,
    )
    if assessor_timed_out:
        discrepancy = DiscrepancyType.ASSESSOR_FALLBACK
    return ShadowRecord(
        trace_id=str(trace_id or "")[:64],
        job_id=str(job_id or "")[:64],
        legacy_requires_orchestration=bool(legacy_requires_orchestration),
        profile_requires_orchestration=bool(profile_requires_orchestration),
        discrepancy_type=discrepancy,
        # 影子期固定 legacy：记录的是"如果切过去会怎样"，不改变实际行为。
        selected_route_source=RouteSource.PROFILE if profile_authoritative else RouteSource.LEGACY,
        legacy_route_mode=str(legacy_route_mode or "")[:40],
        profile_route_mode=str(profile_route_mode or "")[:40],
        legacy_reason_code=str(legacy_reason_code or "")[:80],
        profile_reason_code=str(profile_reason_code or "")[:80],
        confidence_source=str(confidence_source or "")[:24],
        assessor_ms=max(0, int(assessor_ms or 0)),
        assessor_timed_out=bool(assessor_timed_out),
        profile_authoritative=bool(profile_authoritative),
    )


_memory_records: dict[str, list[str]] = {}


async def record_shadow(record: ShadowRecord, *, enabled: bool | None = None) -> bool:
    """写入一条影子记录（失败只记日志；返回是否真的写入）。"""
    active = shadow_enabled() if enabled is None else bool(enabled)
    if not active:
        return False
    if record.differs:
        # 差异是重点观测对象：单独一行 INFO，便于日志侧直接统计。
        logger.info(
            "[task-shadow] 画像差异 type={} legacy={} profile={} job={}",
            str(record.discrepancy_type),
            record.legacy_requires_orchestration,
            record.profile_requires_orchestration,
            str(record.job_id)[:12],
        )
    payload = record.model_dump_json(exclude_none=True)
    job_id = str(record.job_id or "")
    try:
        from app.core.redis import get_redis

        redis = get_redis()
        if job_id:
            key = shadow_key(job_id)
            await redis.rpush(key, payload)
            await redis.ltrim(key, -MAX_RECORDS_PER_JOB, -1)
            await redis.expire(key, _STATS_TTL_SECONDS)
        await redis.hincrby(SHADOW_STATS_KEY, f"type:{record.discrepancy_type}", 1)
        await redis.hincrby(SHADOW_STATS_KEY, "total", 1)
        if record.assessor_timed_out:
            await redis.hincrby(SHADOW_STATS_KEY, "assessor_timeout", 1)
        await redis.expire(SHADOW_STATS_KEY, _STATS_TTL_SECONDS)
        return True
    except Exception as exc:  # noqa: BLE001 - 影子记录失败绝不影响任务
        logger.debug("[task-shadow] Redis 写入不可用，回退内存: {}", str(exc)[:120])
    values = _memory_records.setdefault(job_id or "_", [])
    values.append(payload)
    del values[:-MAX_RECORDS_PER_JOB]
    return True


async def load_shadow_records(job_id: str = "") -> list[ShadowRecord]:
    """读回影子记录（Redis 不可用时读内存；单条损坏跳过）。"""
    out: list[ShadowRecord] = []
    rows: list[Any] = []
    try:
        from app.core.redis import get_redis

        redis = get_redis()
        if job_id:
            rows = await redis.lrange(shadow_key(job_id), 0, -1)
        else:
            rows = await redis.hvals(SHADOW_STATS_KEY)
    except Exception:  # noqa: BLE001
        rows = list(_memory_records.get(job_id or "_", []))
    for raw in rows or []:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or "discrepancy_type" not in payload:
            continue
        try:
            out.append(ShadowRecord.model_validate(payload))
        except Exception:  # noqa: BLE001 - 单条损坏跳过
            continue
    return out


async def shadow_report(
    job_id: str = "",
    *,
    records: list[ShadowRecord] | None = None,
    profile_authoritative: bool = False,
) -> ShadowReport:
    """聚合报告（方案 §5.2：达标才切换）。"""
    rows = records if records is not None else await load_shadow_records(job_id)
    return build_shadow_report(rows, profile_authoritative=profile_authoritative)


async def reset_shadow_for_tests() -> None:
    """清空进程内存记录（显式测试入口；生产不调用）。"""
    _memory_records.clear()


def shadow_snapshot(record: ShadowRecord) -> dict[str, Any]:
    """影子记录 → 可落 routing 的紧凑字段（只放枚举，不放原文）。"""
    return {
        "discrepancy_type": str(record.discrepancy_type),
        "legacy_requires_orchestration": bool(record.legacy_requires_orchestration),
        "profile_requires_orchestration": bool(record.profile_requires_orchestration),
        "selected_route_source": str(record.selected_route_source),
        "profile_authoritative": bool(record.profile_authoritative),
        "assessor_ms": int(record.assessor_ms or 0),
        "assessor_timed_out": bool(record.assessor_timed_out),
    }


def timing_started() -> float:
    """分类耗时起点（单调时钟，避免系统时间回拨导致负值）。"""
    try:
        import time as _time

        return _time.monotonic()
    except Exception:  # noqa: BLE001
        return time.time()


def elapsed_ms(started: float) -> int:
    try:
        import time as _time

        return max(0, int((_time.monotonic() - float(started)) * 1000))
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "MAX_RECORDS_PER_JOB",
    "SHADOW_KEY_PREFIX",
    "SHADOW_STATS_KEY",
    "build_record",
    "elapsed_ms",
    "load_shadow_records",
    "record_shadow",
    "reset_shadow_for_tests",
    "shadow_enabled",
    "shadow_key",
    "shadow_report",
    "shadow_snapshot",
    "timing_started",
]
