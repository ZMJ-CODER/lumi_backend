"""终态封印（方案 §6.2）：任务定局后，迟到的内容类事件一律吞掉。

问题：``POST /jobs/{id}/cancel`` 已经返回，但 ``step_completed`` / ``text_delta``
可能还在路上——前端按"先到先得"渲染就会出现**状态回跳**（已取消的任务又变成运行中）。
这是联调期最难复现的并发 bug，必须在协议层定死：

* **后端两道闸门**：① 同一条流内的 :class:`StreamSeal`（进程内，零成本）；
  ② 任务级 Redis 封印（``job_seal:{job_id}``，跨流/跨进程/补拉同样生效）；
* **只吞"内容类"事件**：``text_delta`` / ``process`` / ``step_*`` / ``tool_*`` /
  ``artifact_created`` / ``view_updated`` / ``approval_required`` 等；
  元数据帧（``title`` / ``summary`` / ``audio_ready`` / ``job`` …）不受影响，
  否则"取消后补标题"这类正常收尾会被误杀；
* **终态帧永远放行**：``control`` 的终态、``done`` / ``task_failed`` / ``cancelled``；
* Redis 不可用时只降级为"流内封印"（实时流仍然正确，补拉可能含迟到帧）。

前端对应规则（写进契约）：收到任何终态事件后 ``isFinal=true``，此后到达的
非终态事件直接丢弃。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from loguru import logger

_SEAL_PREFIX = "job_seal:"
#: 封印 TTL：比事件日志（1h）一致，取消后的任务不需要永久封着。
SEAL_TTL_SECONDS = 3600

#: 终态帧类型（旧投影与标准投影都认）。
TERMINAL_FRAME_TYPES: frozenset[str] = frozenset({
    "done", "task_completed", "task_failed", "cancelled", "job_cancelled", "error_final",
})

#: 终态之后必须被吞掉的"内容类"事件（其余帧照常放行）。
SEAL_BLOCKED_TYPES: frozenset[str] = frozenset({
    "text_delta", "delta", "text", "message", "content",
    "process", "thinking", "thought",
    "step", "step_started", "step_completed", "step_progress",
    "tool", "tool_started", "tool_completed",
    "plan_delta", "plan_ready", "waiting_next",
    "artifact", "artifact_created",
    "view", "view_updated",
    "approval_required", "approval_resolved", "waiting_approval",
    "capability_started", "capability_completed", "capability_failed",
    "capability_requested", "waiting_provider",
    "operation_started", "operation_preview", "operation_completed",
    "operation_failed", "operation_rolled_back",
})

#: ``control`` 的终态取值（与 ``lumi_contracts.events.envelope`` 同一份词表）。
try:  # pragma: no cover - 导入失败时用本地兜底，事件流不受影响
    from lumi_contracts.events.envelope import TERMINAL_CONTROL_STATES as _TERMINAL_STATES
except Exception:  # noqa: BLE001
    _TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "interrupted", "blocked"})  # type: ignore[assignment]


def _key(job_id: str) -> str:
    return f"{_SEAL_PREFIX}{job_id}"


def frame_state(frame: Mapping[str, Any]) -> str:
    """从帧里读"任务状态"（标准投影在 ``payload.state``，旧投影在顶层）。"""
    payload = frame.get("payload")
    values = payload if isinstance(payload, Mapping) else {}
    for source in (values, frame):
        for key in ("state", "job_status", "status"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().casefold()
    return ""


def is_terminal_frame(frame: Mapping[str, Any]) -> bool:
    """帧是否为终态帧（``done`` / ``control`` 终态 / ``task_failed`` / ``cancelled``）。"""
    if not isinstance(frame, Mapping):
        return False
    event_type = str(frame.get("type") or "").strip()
    if event_type in TERMINAL_FRAME_TYPES:
        return True
    if event_type == "control":
        return frame_state(frame) in _TERMINAL_STATES
    # 旧投影的 ``task_failed`` 已在上表；兼容 ``state=...`` 直接写在任意终态帧上的情况。
    if event_type in {"job", "task_router"}:
        return False
    return False


def is_seal_blocked(frame: Mapping[str, Any]) -> bool:
    """终态之后是否必须吞掉这一帧（只有内容类帧会被吞）。"""
    if not isinstance(frame, Mapping):
        return False
    return str(frame.get("type") or "").strip() in SEAL_BLOCKED_TYPES


class StreamSeal:
    """同一条流内的终态封印（进程内，无需 Redis）。

    用法：``kept, dropped = seal.filter(frames)``；出现终态帧后，后续内容类帧
    不再外发（``dropped`` 计数供内部日志）。终态帧本身永远放行。
    """

    __slots__ = ("_sealed_state", "_dropped")

    def __init__(self) -> None:
        self._sealed_state: str = ""
        self._dropped: int = 0

    @property
    def sealed(self) -> bool:
        return bool(self._sealed_state)

    @property
    def state(self) -> str:
        return self._sealed_state

    @property
    def dropped(self) -> int:
        return self._dropped

    def filter(self, frames: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        """过滤一帧列表：返回 ``(放行的帧, 本次吞掉的帧数)``。"""
        kept: list[dict[str, Any]] = []
        dropped = 0
        for frame in frames or []:
            if not isinstance(frame, Mapping):
                continue
            data = dict(frame)
            if self.sealed and is_seal_blocked(data):
                dropped += 1
                continue
            kept.append(data)
            if is_terminal_frame(data):
                self._sealed_state = frame_state(data) or "terminal"
        self._dropped += dropped
        return kept, dropped


async def seal_job(job_id: str, state: str = "cancelled", *, reason_code: str = "") -> bool:
    """把任务标记为定局（取消受理时调用）；之后迟到的内容类事件一律吞掉。"""
    target = str(job_id or "").strip()
    if not target:
        return False
    try:
        from app.core.redis import get_redis

        payload = json.dumps(
            {"state": str(state or "cancelled"), "reason_code": str(reason_code or "")},
            ensure_ascii=False,
        )
        await get_redis().set(_key(target), payload, ex=SEAL_TTL_SECONDS)
        return True
    except Exception as exc:  # noqa: BLE001 - 封印不可用不能影响取消本身
        logger.debug("[event-seal] 写入失败（降级为流内封印）: {}", str(exc)[:120])
        return False


async def unseal_job(job_id: str) -> None:
    """解除封印（任务被恢复/重跑时用；正常任务不需要）。"""
    target = str(job_id or "").strip()
    if not target:
        return
    try:
        from app.core.redis import get_redis

        await get_redis().delete(_key(target))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[event-seal] 解除失败: {}", str(exc)[:120])


async def job_seal_state(job_id: str) -> str:
    """任务的封印状态（``""`` = 未封印）。Redis 不可用时按"未封印"处理。"""
    target = str(job_id or "").strip()
    if not target:
        return ""
    try:
        from app.core.redis import get_redis

        raw = await get_redis().get(_key(target))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[event-seal] 读取失败（按未封印处理）: {}", str(exc)[:120])
        return ""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    return str(data.get("state") or "cancelled") if isinstance(data, dict) else "cancelled"


async def filter_frames_for_job(
    frames: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """按**任务级封印**过滤帧（写事件日志前用：补拉也不能含迟到内容帧）。

    需要 Redis 查询，因此只在确实存在封印的任务上产生额外开销（一次 GET/job）。
    """
    if not frames:
        return [], 0
    states: dict[str, str] = {}
    for frame in frames:
        job_id = str(frame.get("job_id") or "")
        if job_id and job_id not in states:
            states[job_id] = await job_seal_state(job_id)
    sealed_jobs = {job_id for job_id, state in states.items() if state}
    if not sealed_jobs:
        return list(frames), 0
    kept: list[dict[str, Any]] = []
    dropped = 0
    for frame in frames:
        job_id = str(frame.get("job_id") or "")
        if job_id in sealed_jobs and not is_terminal_frame(frame) and is_seal_blocked(frame):
            dropped += 1
            continue
        kept.append(frame)
    if dropped:
        logger.debug("[event-seal] 任务已定局，吞掉 {} 帧迟到事件: {}", dropped, sorted(sealed_jobs))
    return kept, dropped


__all__ = [
    "SEAL_BLOCKED_TYPES",
    "SEAL_TTL_SECONDS",
    "TERMINAL_FRAME_TYPES",
    "StreamSeal",
    "filter_frames_for_job",
    "frame_state",
    "is_seal_blocked",
    "is_terminal_frame",
    "job_seal_state",
    "seal_job",
    "unseal_job",
]
