"""``job_runs`` / ``job_steps`` 的**异步数据库投影**（方案 §3.3）。

定位极其重要：这两张表**不是实时事实源**。

* 事实源 = Redis Job State + ResultStore + Effect Journal；
* 本模块把检查点批量、幂等地投影进 DB，只服务于"查询与审计"；
* 因此：**每个 Token 都不许同步写 DB**；DB 写失败**不阻塞任务执行**，恢复后可补写。

实现要点：

* :class:`JobProjector` 在内存里攒待写记录，按 ``flush_interval`` / ``batch_size``
  批量提交（``asyncio.Queue`` 式，但不需要后台任务——写入失败只记日志并保留待补写）；
* 幂等 upsert：``job_runs`` 按主键 ``job_id``，``job_steps`` 按唯一键
  ``(job_id, step_id, attempt)``，重复投影只更新，不产生重复行；
* **旧检查点不覆盖新状态**：``job_steps.checkpoint_version`` 更小时直接跳过该行
  （与契约 ``merge_checkpoint`` 同一规则，避免乱序投影把状态写回去）；
* 单例 ``projector()`` 供执行链路使用；DB 不可用（未初始化/无连接）时整体降级为空操作。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from typing import Any

from loguru import logger

#: 攒批参数（不写死"每个事件一次往返"）。
DEFAULT_BATCH_SIZE = 50
DEFAULT_FLUSH_INTERVAL_SECONDS = 2.0
#: 待补写队列上限（防止 DB 长时间不可用把内存撑爆）。
MAX_PENDING = 2000


def projection_enabled() -> bool:
    """是否启用 DB 投影（``JOB_PROJECTION_ENABLED``，默认关闭：先跑通 Redis 路径）。"""
    try:
        from app.core.config import settings

        return bool(getattr(settings, "JOB_PROJECTION_ENABLED", False))
    except Exception:  # noqa: BLE001 - 配置不可用时保持关闭（保守）
        return False


@dataclass(slots=True)
class _RunRow:
    job_id: str
    user_id: str = ""
    conversation_id: str = ""
    status: str = ""
    current_step_id: str = ""
    plan_revision: int = 1
    last_checkpoint_version: int = 0
    error_code: str = ""
    queued_at: float = 0.0


@dataclass(slots=True)
class _StepRow:
    job_id: str
    step_id: str
    attempt: int = 1
    tool_name: str = ""
    step_type: str = ""
    status: str = ""
    input_digest: str = ""
    output_summary: str = ""
    result_ref: dict[str, Any] | None = None
    artifact_refs: list[dict[str, Any]] = field(default_factory=list)
    error_code: str = ""
    effect_status: str = ""
    checkpoint_version: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    queued_at: float = 0.0


def rows_from_checkpoint(checkpoint: Any) -> _StepRow:
    """契约 ``StepCheckpoint`` → 投影行。"""
    return _StepRow(
        job_id=str(getattr(checkpoint, "job_id", "") or ""),
        step_id=str(getattr(checkpoint, "step_id", "") or ""),
        attempt=int(getattr(checkpoint, "attempt", 1) or 1),
        tool_name=str(getattr(checkpoint, "tool_name", "") or "")[:160],
        step_type=str(getattr(checkpoint, "step_type", "") or "")[:80],
        status=str(getattr(getattr(checkpoint, "status", ""), "value", getattr(checkpoint, "status", "")) or ""),
        input_digest=str(getattr(checkpoint, "input_digest", "") or "")[:128],
        output_summary=str(getattr(checkpoint, "output_summary", "") or "")[:4000],
        result_ref=(
            dict(getattr(checkpoint, "result_ref", None))
            if isinstance(getattr(checkpoint, "result_ref", None), dict)
            else None
        ),
        artifact_refs=[
            dict(item)
            for item in (getattr(checkpoint, "artifact_refs", None) or [])
            if isinstance(item, dict)
        ],
        error_code=str(getattr(checkpoint, "error_code", "") or "")[:120],
        effect_status=str(getattr(checkpoint, "effect_status", "") or "")[:24],
        checkpoint_version=int(getattr(checkpoint, "checkpoint_version", 0) or 0),
        started_at=float(getattr(checkpoint, "started_at", 0.0) or 0.0),
        finished_at=float(getattr(checkpoint, "finished_at", 0.0) or 0.0),
        queued_at=time.time(),
    )


class JobProjector:
    """攒批 + 幂等 upsert 的投影器（DB 失败不阻塞、可补写）。"""

    def __init__(
        self,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        flush_interval: float = DEFAULT_FLUSH_INTERVAL_SECONDS,
        enabled: bool | None = None,
        session_factory: Any = None,
        clock: Any = None,
    ) -> None:
        self._batch = max(1, int(batch_size))
        self._interval = max(0.0, float(flush_interval))
        self._enabled = projection_enabled() if enabled is None else bool(enabled)
        self._session_factory = session_factory
        self._clock = clock or time.time
        self._runs: dict[str, _RunRow] = {}
        self._steps: dict[tuple[str, str, int], _StepRow] = {}
        # 时间水位从构造时刻起算：第一次 flush 不会被"时钟从 0 开始"误判为到期。
        self._last_flush = float(self._clock())
        self._lock = asyncio.Lock()
        self._failures = 0
        self._flushed = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def pending(self) -> int:
        return len(self._runs) + len(self._steps)

    @property
    def stats(self) -> dict[str, int]:
        return {"pending": self.pending, "flushed": self._flushed, "failures": self._failures}

    def queue_run(self, row: _RunRow) -> None:
        """把一条任务控制面行放进待写队列（同 job 只保留最新）。"""
        if not self._enabled or not row.job_id:
            return
        current = self._runs.get(row.job_id)
        if current is None:
            self._runs[row.job_id] = row
            return
        merged = replace(current)
        for name in (
            "user_id",
            "conversation_id",
            "status",
            "current_step_id",
            "plan_revision",
            "error_code",
        ):
            value = getattr(row, name)
            if value not in ("", 0, None):
                setattr(merged, name, value)
        # 状态 / 当前步骤 / 错误码是"最新即事实"：即使为空也要覆盖（例如错误已清除）。
        merged.status = row.status or current.status
        merged.current_step_id = row.current_step_id or current.current_step_id
        merged.error_code = row.error_code
        merged.last_checkpoint_version = max(
            int(current.last_checkpoint_version or 0), int(row.last_checkpoint_version or 0)
        )
        self._runs[row.job_id] = merged

    def queue_step(self, row: _StepRow) -> None:
        """把一条步骤检查点行放进待写队列（同 ``(job, step, attempt)`` 只保留最新）。"""
        if not self._enabled or not row.job_id or not row.step_id:
            return
        key = (row.job_id, row.step_id, int(row.attempt or 1))
        current = self._steps.get(key)
        if current is not None and int(current.checkpoint_version or 0) > int(row.checkpoint_version or 0):
            # 旧检查点不得覆盖新状态（与契约 merge_checkpoint 同一规则）。
            return
        self._steps[key] = row

    def should_flush(self) -> bool:
        if self.pending >= self._batch:
            return True
        if not self.pending:
            return False
        return (float(self._clock()) - self._last_flush) >= self._interval

    async def flush(self, *, force: bool = False) -> int:
        """批量提交待写队列；失败保留待补写并记日志（绝不抛给调用方）。"""
        if not self._enabled:
            return 0
        if not force and not self.should_flush():
            return 0
        async with self._lock:
            runs, steps = self._runs, self._steps
            if not runs and not steps:
                self._last_flush = float(self._clock())
                return 0
            self._runs, self._steps = {}, {}
            try:
                written = await self._write(runs, steps)
            except Exception as exc:  # noqa: BLE001 - DB 写失败不阻塞任务执行
                self._failures += 1
                self._last_flush = float(self._clock())
                self._requeue(runs, steps)
                logger.debug("[job-projection] 投影失败（待补写）: {}", str(exc)[:160])
                return 0
            self._flushed += written
            self._last_flush = float(self._clock())
            return written

    def _requeue(self, runs: dict[str, _RunRow], steps: dict[tuple[str, str, int], _StepRow]) -> None:
        """失败后把待写行放回（有界：超限丢最旧的，绝不无限增长）。"""
        for key, row in runs.items():
            self._runs.setdefault(key, row)
        for key, row in steps.items():
            self._steps.setdefault(key, row)
        while self.pending > MAX_PENDING:
            if self._runs:
                self._runs.pop(next(iter(self._runs)), None)
                continue
            self._steps.pop(next(iter(self._steps)), None)

    async def _write(self, runs: dict[str, _RunRow], steps: dict[tuple[str, str, int], _StepRow]) -> int:
        """真正的 upsert（独立会话 + 单事务；冲突只更新）。"""
        from sqlalchemy.dialects.postgresql import insert

        from app.core.database import async_session_factory
        from app.models.db_models import JobRun, JobStep

        factory = self._session_factory or async_session_factory
        written = 0
        async with factory() as session:
            async with session.begin():
                for row in runs.values():
                    statement = insert(JobRun).values(
                        job_id=row.job_id,
                        user_id=row.user_id,
                        conversation_id=row.conversation_id,
                        status=row.status or "pending",
                        current_step_id=row.current_step_id,
                        plan_revision=int(row.plan_revision or 1),
                        last_checkpoint_version=int(row.last_checkpoint_version or 0),
                        error_code=row.error_code,
                    )
                    await session.execute(
                        statement.on_conflict_do_update(
                            index_elements=[JobRun.job_id],
                            set_={
                                "user_id": statement.excluded.user_id,
                                "conversation_id": statement.excluded.conversation_id,
                                "status": statement.excluded.status,
                                "current_step_id": statement.excluded.current_step_id,
                                "plan_revision": statement.excluded.plan_revision,
                                "last_checkpoint_version": statement.excluded.last_checkpoint_version,
                                "error_code": statement.excluded.error_code,
                            },
                        )
                    )
                    written += 1
                for row in steps.values():
                    statement = insert(JobStep).values(
                        job_id=row.job_id,
                        step_id=row.step_id,
                        attempt=int(row.attempt or 1),
                        tool_name=row.tool_name,
                        step_type=row.step_type,
                        status=row.status or "planned",
                        input_digest=row.input_digest,
                        output_summary=row.output_summary,
                        result_ref=row.result_ref,
                        artifact_refs=list(row.artifact_refs or []),
                        error_code=row.error_code,
                        effect_status=row.effect_status,
                        checkpoint_version=int(row.checkpoint_version or 0),
                        started_at=_dt(row.started_at),
                        finished_at=_dt(row.finished_at),
                    )
                    await session.execute(
                        statement.on_conflict_do_update(
                            index_elements=[JobStep.job_id, JobStep.step_id, JobStep.attempt],
                            set_={
                                "tool_name": statement.excluded.tool_name,
                                "step_type": statement.excluded.step_type,
                                "status": statement.excluded.status,
                                "input_digest": statement.excluded.input_digest,
                                "output_summary": statement.excluded.output_summary,
                                "result_ref": statement.excluded.result_ref,
                                "artifact_refs": statement.excluded.artifact_refs,
                                "error_code": statement.excluded.error_code,
                                "effect_status": statement.excluded.effect_status,
                                "checkpoint_version": statement.excluded.checkpoint_version,
                                "started_at": statement.excluded.started_at,
                                "finished_at": statement.excluded.finished_at,
                            },
                        )
                    )
                    written += 1
        return written


def _dt(timestamp: float) -> Any:
    if not timestamp:
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc)


_projector: JobProjector | None = None


def projector() -> JobProjector:
    """进程级单例（执行链路共用同一份攒批队列）。"""
    global _projector
    if _projector is None:
        _projector = JobProjector()
    return _projector


def set_projector_for_tests(instance: JobProjector | None) -> None:
    """显式测试替身；生产代码不调用。"""
    global _projector
    _projector = instance


async def project_job_run(
    *,
    job_id: str,
    user_id: str = "",
    conversation_id: str = "",
    status: str = "",
    current_step_id: str = "",
    plan_revision: int = 1,
    last_checkpoint_version: int = 0,
    error_code: str = "",
) -> None:
    """投影一条任务控制面记录（入队 + 按需 flush；失败不影响执行）。"""
    active = projector()
    if not active.enabled:
        return
    active.queue_run(
        _RunRow(
            job_id=str(job_id or ""),
            user_id=str(user_id or ""),
            conversation_id=str(conversation_id or ""),
            status=str(status or ""),
            current_step_id=str(current_step_id or ""),
            plan_revision=int(plan_revision or 1),
            last_checkpoint_version=int(last_checkpoint_version or 0),
            error_code=str(error_code or ""),
            queued_at=time.time(),
        )
    )
    await active.flush()


async def project_step_checkpoints(job_id: str, checkpoints: Any) -> None:
    """投影一批步骤检查点（入队 + 按需 flush；失败不影响执行）。"""
    active = projector()
    if not active.enabled:
        return
    for item in checkpoints or ():
        active.queue_step(rows_from_checkpoint(item))
    await active.flush()


async def flush_projections() -> int:
    """强制提交（任务终态 / 进程退出前调用，保证"恢复后可补写"之外的正常收尾）。"""
    return await projector().flush(force=True)


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_FLUSH_INTERVAL_SECONDS",
    "MAX_PENDING",
    "JobProjector",
    "flush_projections",
    "project_job_run",
    "project_step_checkpoints",
    "projection_enabled",
    "projector",
    "rows_from_checkpoint",
    "set_projector_for_tests",
]
