"""步骤检查点协调器（方案 §2）：执行器外层的**唯一**检查点写入点。

为什么不让每个 Tool / Skill 自己保存状态：那样每种执行路径都会长出一套"我完成了"
的判定，恢复时就必须同时信任 N 套口径。这里把检查点收口成一个协调器，并把它接入
**既有的** ``StepRunService.save_state``（每次步骤状态落库都会经过它），因此：

* 不新增第二套执行状态；
* 检查点在"结果已落 ResultStore、状态已写 Job"之后写入，
  并且**完成事件（step_completed / task_failed）在检查点落盘之后才发送**——
  这条铁律由 `STEP_CHECKPOINT_V2` 的接线顺序保证（见 ``StepRunService.save_state``：
  保存状态 → 写检查点 → 内核对已保存状态发射完成事件）。

状态机（``lumi_contracts.persistence.checkpoint``）：

    planned → started → running → waiting_approval
            → completed | failed | cancelled | uncertain

``uncertain`` 是**终态**：副作用可能已发生但无法确认，绝不自动重跑，只能由策略或
人工决策（方案 §4.3）。

落盘位置：``multiagent:step_checkpoints:{job_id}`` 的 Redis hash（字段 = ``step_id``），
与 Job 快照分离——因此写检查点不会让 Job 快照膨胀（方案 §3 快照硬边界）。
Redis 不可用时回退进程内存：**检查点写失败不阻塞任务执行**，但会记日志。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from loguru import logger

from lumi_contracts.persistence.checkpoint import (
    StepCheckpoint,
    StepCheckpointState,
    checkpoint_summary,
    is_regression,
    normalize_state,
    runtime_status_for,
    unfinished_steps,
    unresolved_side_effects,
)

#: 检查点 Redis 键前缀（与 Job 快照键分离，避免快照膨胀）。
CHECKPOINT_KEY_PREFIX = "multiagent:step_checkpoints:"


def checkpoint_key(job_id: str) -> str:
    return f"{CHECKPOINT_KEY_PREFIX}{job_id}"


def checkpoint_ttl_seconds() -> int:
    """检查点 TTL：与 Job 快照同源（任务状态没了，检查点也没意义）。"""
    try:
        from app.core.config import settings

        return max(
            3600,
            int(settings.AGENT_JOBS_TTL_SECONDS),
            int(settings.AGENT_RESULT_REF_TTL_SECONDS),
        )
    except Exception:  # noqa: BLE001
        return 86400


def checkpoints_enabled() -> bool:
    """``STEP_CHECKPOINT_V2``（默认关闭；关闭时写入是空操作）。"""
    try:
        from app.core.feature_flags import feature_enabled

        return feature_enabled("STEP_CHECKPOINT_V2")
    except Exception:  # noqa: BLE001 - 开关不可用时保持旧行为
        return False


# ── 落盘端口 ─────────────────────────────────────────────────


class CheckpointStore(Protocol):
    """检查点落盘端口（Redis hash / 进程内存 / 将来的表投影）。"""

    async def put(self, checkpoint: StepCheckpoint) -> None: ...

    async def load(self, job_id: str) -> list[StepCheckpoint]: ...

    async def clear(self, job_id: str) -> None: ...


class RedisCheckpointStore:
    """Redis hash 落盘（字段 = ``step_id``，同一步骤覆盖写）。"""

    def __init__(self, *, redis: Any = None, ttl_seconds: int | None = None) -> None:
        self._redis = redis
        self._ttl = int(ttl_seconds if ttl_seconds is not None else checkpoint_ttl_seconds())

    def _client(self) -> Any:
        if self._redis is not None:
            return self._redis
        from app.core.redis import get_redis

        return get_redis()

    async def put(self, checkpoint: StepCheckpoint) -> None:
        client = self._client()
        key = checkpoint_key(checkpoint.job_id)
        await client.hset(key, str(checkpoint.step_id), checkpoint.model_dump_json(exclude_none=True))
        await client.expire(key, self._ttl)

    async def load(self, job_id: str) -> list[StepCheckpoint]:
        client = self._client()
        raw = await client.hgetall(checkpoint_key(str(job_id)))
        return _decode_rows(raw.values() if isinstance(raw, dict) else raw or [])

    async def clear(self, job_id: str) -> None:
        await self._client().delete(checkpoint_key(str(job_id)))


class InMemoryCheckpointStore:
    """进程内存落盘（Redis 不可用 / 单测）：只保证同一进程内可读，不伪装持久化。"""

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, str]] = {}

    async def put(self, checkpoint: StepCheckpoint) -> None:
        bucket = self._rows.setdefault(str(checkpoint.job_id), {})
        bucket[str(checkpoint.step_id)] = checkpoint.model_dump_json(exclude_none=True)

    async def load(self, job_id: str) -> list[StepCheckpoint]:
        return _decode_rows(list(self._rows.get(str(job_id), {}).values()))

    async def clear(self, job_id: str) -> None:
        self._rows.pop(str(job_id), None)


def _decode_rows(rows: Any) -> list[StepCheckpoint]:
    out: list[StepCheckpoint] = []
    for raw in rows or []:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        if not raw:
            continue
        try:
            out.append(StepCheckpoint.model_validate_json(raw))
        except Exception:  # noqa: BLE001 - 单条损坏跳过，不让整批读失败
            continue
    return out


_memory_store = InMemoryCheckpointStore()


def default_checkpoint_store() -> CheckpointStore:
    """默认落盘实现：Redis 可用时用 Redis，否则进程内存（不抛错）。"""
    try:
        from app.core.redis import get_redis

        return RedisCheckpointStore(redis=get_redis())
    except Exception:  # noqa: BLE001 - Redis 未初始化（测试/本地）走内存
        return _memory_store


# ── 协调器 ───────────────────────────────────────────────────

#: 检查点写入结果（调用方据此判断"完成事件能否发"）。
@dataclass(frozen=True, slots=True)
class CheckpointWrite:
    persisted: bool = False
    checkpoint_version: int = 0
    state: str = ""
    error: str = ""
    #: 已落盘的检查点（异步 DB 投影直接消费它，不必二次读取）。
    checkpoint: StepCheckpoint | None = None


@dataclass(slots=True)
class _StepCursor:
    """单个步骤在本轮执行里的版本游标。"""

    checkpoint_version: int = 0
    persisted_version: int = 0


class StepCheckpointCoordinator:
    """每步检查点的统一记录器（方案 §2.1 / §2.3）。"""

    def __init__(
        self,
        *,
        job_id: str,
        store: CheckpointStore | None = None,
        enabled: bool | None = None,
        clock: Any = None,
        max_cached_steps: int = 500,
    ) -> None:
        self._job_id = str(job_id or "")
        self._store = store if store is not None else default_checkpoint_store()
        self._enabled = checkpoints_enabled() if enabled is None else bool(enabled)
        self._clock = clock or time.time
        self._max = max(1, int(max_cached_steps))
        self._cursors: dict[str, _StepCursor] = {}
        self._known: dict[str, StepCheckpoint] = {}
        self._events: dict[str, int] = {}

    @property
    def job_id(self) -> str:
        return self._job_id

    @property
    def enabled(self) -> bool:
        return self._enabled and bool(self._job_id)

    # ── 完成事件时序（方案 §2.3 铁律）───────────────────────

    async def confirm_persisted_for_emit(self, step_id: str, event_type: str) -> int:
        """发完成事件前的唯一闸门：返回**已落盘**的检查点版本。

        返回 ``0`` 表示该步骤的检查点还没落盘（结果引用可能还没写成功）——
        调用方据此延迟/拒绝发送完成事件。事件计数在同一处推进，便于排障
        对比"发了多少完成事件 / 落了多少检查点"。
        """
        version = self.persisted_version(step_id)
        key = str(event_type or "")
        if version > 0:
            self._events[key] = self._events.get(key, 0) + 1
        return version

    def emitted_events(self) -> dict[str, int]:
        """本协调器确认过的完成事件计数（排障/指标用）。"""
        return dict(self._events)

    async def load(self) -> list[StepCheckpoint]:
        """读回本任务的检查点（Redis/内存不可用时返回空列表）。"""
        try:
            rows = await self._store.load(self._job_id)
        except Exception as exc:  # noqa: BLE001 - 读不到检查点不阻塞执行
            logger.debug("[checkpoint] 读取失败 job={}: {}", self._job_id[:12], str(exc)[:120])
            return []
        for item in rows:
            self._known[item.key] = item
        return rows

    async def record(
        self,
        step_id: str,
        status: StepCheckpointState | str,
        *,
        attempt: int = 0,
        **fields: Any,
    ) -> CheckpointWrite:
        """写入一次检查点（非法跃迁记日志并拒绝，不写坏状态）。"""
        if not self.enabled or not str(step_id or ""):
            return CheckpointWrite(persisted=False, state=str(status))
        target = normalize_state(status)
        if target is None:
            return CheckpointWrite(persisted=False, error=f"未知步骤状态：{status!r}")
        key = str(step_id)
        cursor = self._cursors.setdefault(key, _StepCursor())
        current = self._known.get((self._job_id, key, int(attempt or 1))) or self._latest(key)
        if current is None:
            current = StepCheckpoint(
                job_id=self._job_id,
                step_id=key,
                attempt=max(1, int(attempt or 1)),
                checkpoint_version=int(cursor.persisted_version or 0),
                updated_at=float(self._clock()),
            )
        elif is_regression(current.state, target):
            # 既有内核允许步骤状态回滚（写资源不可用时 ``revert_to_waiting``），但检查点
            # 是**结论**：一旦推进就不回退，否则恢复时会看到"跑过的步骤又没跑"并重复副作用。
            logger.debug(
                "[checkpoint] 忽略状态回退 job={} step={} {} → {}",
                self._job_id[:12],
                key[:24],
                current.state.value,
                target.value,
            )
            return CheckpointWrite(
                persisted=False,
                checkpoint_version=int(cursor.persisted_version or 0),
                state=current.state.value,
                error="state_regression_ignored",
                checkpoint=current,
            )
        try:
            advanced = current.advanced_to(target, now=float(self._clock()), **fields)
        except ValueError as exc:
            logger.warning(
                "[checkpoint] 状态跃迁被拒绝 job={} step={} {}: {}",
                self._job_id[:12],
                key[:24],
                current.state.value,
                str(exc)[:120],
            )
            return CheckpointWrite(
                persisted=False,
                checkpoint_version=int(current.checkpoint_version or 0),
                state=current.state.value,
                error=str(exc),
            )
        try:
            await self._store.put(advanced)
        except Exception as exc:  # noqa: BLE001 - 检查点写失败不阻塞执行，但必须留痕
            logger.warning(
                "[checkpoint] 写入失败 job={} step={}: {}", self._job_id[:12], key[:24], str(exc)[:160]
            )
            return CheckpointWrite(
                persisted=False,
                checkpoint_version=int(current.checkpoint_version or 0),
                state=advanced.state.value,
                error=str(exc),
            )
        self._known[advanced.key] = advanced
        cursor.checkpoint_version = int(advanced.checkpoint_version or 0)
        cursor.persisted_version = int(advanced.checkpoint_version or 0)
        self._trim()
        return CheckpointWrite(
            persisted=True,
            checkpoint_version=int(advanced.checkpoint_version or 0),
            state=advanced.state.value,
            checkpoint=advanced,
        )

    def _latest(self, step_id: str) -> StepCheckpoint | None:
        rows = [item for item in self._known.values() if item.step_id == step_id]
        if not rows:
            return None
        return max(rows, key=lambda item: int(item.checkpoint_version or 0))

    def _trim(self) -> None:
        if len(self._known) <= self._max:
            return
        ordered = sorted(self._known.items(), key=lambda pair: int(pair[1].checkpoint_version or 0))
        for key, _ in ordered[: len(ordered) - self._max]:
            self._known.pop(key, None)

    def persisted_version(self, step_id: str) -> int:
        """已落盘版本（发完成事件前的时序校验用）。"""
        cursor = self._cursors.get(str(step_id))
        if cursor is not None:
            return int(cursor.persisted_version or 0)
        item = self._latest(str(step_id))
        return int(item.checkpoint_version or 0) if item is not None else 0

    def checkpoint_for(self, step_id: str) -> StepCheckpoint | None:
        """当前已知的检查点（回退被忽略时也返回**已推进**的那一份）。"""
        return self._latest(str(step_id))

    async def fail_closed_uncertain(self, step_id: str, reason: str) -> CheckpointWrite:
        """把步骤标为 ``uncertain``（副作用可能已发生，绝不自动重跑）。"""
        return await self.record(
            step_id,
            StepCheckpointState.UNCERTAIN,
            error_code="EFFECT_UNCERTAIN",
            output_summary=str(reason or "")[:200],
        )


# ── 与既有步骤状态的映射（不改既有状态字符串）────────────────


def state_for_step(*, status: str, job_status: str = "") -> StepCheckpointState:
    """既有步骤状态（``routing.steps[].status``）→ 检查点状态。"""
    text = str(status or "").strip().lower()
    mapping: dict[str, StepCheckpointState] = {
        "pending": StepCheckpointState.PLANNED,
        "running": StepCheckpointState.RUNNING,
        "waiting_approval": StepCheckpointState.WAITING_APPROVAL,
        "completed": StepCheckpointState.COMPLETED,
        "failed": StepCheckpointState.FAILED,
        "skipped": StepCheckpointState.CANCELLED,
        "cancelled": StepCheckpointState.CANCELLED,
        "uncertain": StepCheckpointState.UNCERTAIN,
    }
    if text in mapping:
        return mapping[text]
    job = str(job_status or "").strip().lower()
    if job == "cancelled":
        return StepCheckpointState.CANCELLED
    return StepCheckpointState.PLANNED


def checkpoint_view_fields(checkpoints: list[StepCheckpoint]) -> dict[str, Any]:
    """给运行视图/接口用的检查点摘要（只放计数与最大版本，不放正文）。"""
    if not checkpoints:
        return {}
    return {
        "checkpoint_summary": checkpoint_summary(checkpoints),
        "runtime_status_by_step": {
            str(item.step_id): runtime_status_for(item.state) for item in checkpoints
        },
    }


async def load_checkpoints(job_id: str, *, store: CheckpointStore | None = None) -> list[StepCheckpoint]:
    """读取某任务的检查点（恢复流程入口）。"""
    active = store if store is not None else default_checkpoint_store()
    try:
        return await active.load(str(job_id or ""))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[checkpoint] 读取失败 job={}: {}", str(job_id)[:12], str(exc)[:120])
        return []


# ── 任务级活跃协调器（供"发完成事件前查已落盘版本"使用）──────

_active: dict[str, StepCheckpointCoordinator] = {}
_ACTIVE_MAX_JOBS = 256


def coordinator_for(job_id: str) -> StepCheckpointCoordinator:
    """取（或建）任务的活跃协调器：同一任务的所有写入共用版本游标。

    只保留最近 ``_ACTIVE_MAX_JOBS`` 个任务：检查点本身落在 Redis，这里的字典只是
    "本进程内正在执行的任务"的版本游标缓存，不是事实源。
    """
    key = str(job_id or "")
    found = _active.get(key)
    if found is not None:
        return found
    created = StepCheckpointCoordinator(job_id=key)
    if len(_active) >= _ACTIVE_MAX_JOBS:
        for stale in list(_active)[: len(_active) - _ACTIVE_MAX_JOBS + 1]:
            _active.pop(stale, None)
    _active[key] = created
    return created


def drop_coordinator(job_id: str) -> None:
    """任务结束后释放进程内游标（检查点仍在 Redis）。"""
    _active.pop(str(job_id or ""), None)


def set_coordinator_for_tests(job_id: str, coordinator: StepCheckpointCoordinator | None) -> None:
    """显式测试替身；生产代码不调用。"""
    key = str(job_id or "")
    if coordinator is None:
        _active.pop(key, None)
        return
    _active[key] = coordinator


@dataclass(frozen=True, slots=True)
class CheckpointRecoveryView:
    """恢复所需的最小检查点视图（方案 §5 第 2/4 步）。"""

    checkpoints: tuple[StepCheckpoint, ...] = field(default_factory=tuple)
    max_checkpoint_version: int = 0

    @property
    def unfinished_step_ids(self) -> tuple[str, ...]:
        return tuple(item.step_id for item in unfinished_steps(self.checkpoints))

    @property
    def unresolved_effect_step_ids(self) -> tuple[str, ...]:
        return tuple(item.step_id for item in unresolved_side_effects(self.checkpoints))

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_checkpoint_version": self.max_checkpoint_version,
            "unfinished_step_ids": list(self.unfinished_step_ids),
            "unresolved_effect_step_ids": list(self.unresolved_effect_step_ids),
            "summary": checkpoint_summary(self.checkpoints),
        }


def recovery_view(checkpoints: list[StepCheckpoint]) -> CheckpointRecoveryView:
    rows = tuple(checkpoints or ())
    return CheckpointRecoveryView(
        checkpoints=rows,
        max_checkpoint_version=max((int(item.checkpoint_version or 0) for item in rows), default=0),
    )


def checkpoint_debug_payload(checkpoints: list[StepCheckpoint]) -> str:
    """排障用的一行 JSON（不含正文，只含状态与引用 id）。"""
    return json.dumps(
        [
            {
                "step_id": item.step_id,
                "state": item.state.value,
                "version": int(item.checkpoint_version or 0),
                "effect_status": item.effect_status,
                "error_code": item.error_code,
            }
            for item in checkpoints
        ],
        ensure_ascii=False,
    )


__all__ = [
    "CHECKPOINT_KEY_PREFIX",
    "CheckpointRecoveryView",
    "CheckpointStore",
    "CheckpointWrite",
    "InMemoryCheckpointStore",
    "RedisCheckpointStore",
    "StepCheckpointCoordinator",
    "checkpoint_debug_payload",
    "checkpoint_key",
    "checkpoint_ttl_seconds",
    "checkpoint_view_fields",
    "checkpoints_enabled",
    "coordinator_for",
    "default_checkpoint_store",
    "drop_coordinator",
    "load_checkpoints",
    "recovery_view",    "set_coordinator_for_tests",
    "state_for_step",
]
