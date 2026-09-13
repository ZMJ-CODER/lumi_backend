"""断线恢复：**先读快照，再补增量**（方案 §6：消除 Snapshot 真空期）。

## 坑在哪

客户端检测到 ``SEQ_GAP`` 时会请求恢复。如果实现成"同步遍历 Event Log 重建 Snapshot"，
这个请求就会阻塞数秒到数十秒（取决于日志量与实现），而这**恰好发生在用户最着急的时刻**
（刚掉线重连）。

## 做法：快照就是检查点

本仓库已经有现成的两件东西，恢复路径只是把它们**按水位拼起来**，不再重新计算：

1. **运行视图快照**（由 ``SnapshotWriter`` 在每次步骤状态落库时写入，键只在
   ``app.services.job_snapshot_store`` 出现）：它自带 ``last_seq``，因此天然就是
   "某个事件水位上的检查点"——恢复路径不需要重建视图，也不需要认识快照键；
2. **事件日志**（``job_events:{job_id}`` 的 Redis list）：按 ``seq`` 可切片。

于是恢复 = 一次 ``GET``（快照）+ 一次有界 ``LRANGE``（水位之后的增量），毫秒级。
**不重放历史**、**不重建视图**、**不遍历全量日志**。

## 协议

:func:`build_gap_recovery` 返回一个自洽的恢复包：

* ``snapshot``：``JobRunView`` 快照（形状与 ``GET /jobs/{id}`` 里的 ``run_view`` 一致），
  可能为 ``None``（快照过期/未写过）——此时前端必须走既有 ``GET /jobs/{id}`` 全量恢复；
* ``baseline_seq``：快照覆盖到的事件水位。客户端应**丢弃自己 lastSeq 之前的**所有本地状态，
  从 ``baseline_seq`` 重新对齐；
* ``events``：``seq > baseline_seq`` 的增量帧（与实时流同一份标准帧，可直接按
  ``event_id`` 去重后追加）；
* ``head_seq`` / ``truncated`` / ``retry_after_ms``：还要不要继续拉、是否被 limit 截断。

``resume_mode`` 让客户端一眼看出该怎么走：

* ``snapshot_delta``：有快照 + 增量（最理想，毫秒级）；
* ``snapshot_only``：有快照、没有增量（已经追平）；
* ``events_only``：没有快照（过期/未写过），只能靠增量 + 既有全量接口；
* ``full_refetch``：连事件都没有（日志过期或 Redis 降级），必须走 ``GET /jobs/{id}``。
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

#: 单次恢复返回的最大增量帧数（与 ``/events`` 的上限一致，避免一次返回过大）。
DEFAULT_RESUME_LIMIT = 500

#: Redis 降级/日志过期时的建议重试间隔：让客户端不要疯狂轮询。
RETRY_AFTER_MS = 2000

#: 工具窗口诊断的 Redis 键前缀与保留条数（只留最近的窗口，避免无限增长）。
TOOL_WINDOW_KEY_PREFIX = "tool_window:"
TOOL_WINDOW_MAX_ENTRIES = 20
TOOL_WINDOW_TTL_SECONDS = 24 * 3600


def tool_window_key(job_id: str) -> str:
    return f"{TOOL_WINDOW_KEY_PREFIX}{job_id}"


async def record_tool_window(job_id: str, payload: dict[str, Any]) -> None:
    """把一次工具窗口快照追加到任务级列表（**诊断数据，失败静默**）。

    为什么值得落盘：工具"在哪一层消失"是线上最难问的问题之一（模型说工具不可用，
    日志里只有一行 INFO）。落成按任务可读的结构化证据后，前端可以把它做成排障面板，
    运维不用再翻服务端日志。
    """
    target = str(job_id or "")
    if not target or not payload:
        return
    try:
        from app.core.redis import get_redis

        client = get_redis()
        key = tool_window_key(target)
        await client.rpush(key, json.dumps(payload, ensure_ascii=False, default=str))
        await client.ltrim(key, -TOOL_WINDOW_MAX_ENTRIES, -1)
        await client.expire(key, TOOL_WINDOW_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 - 诊断写入绝不能影响工具注入
        logger.debug("[tool-window] 诊断快照写入失败 job={}: {}", target[:12], str(exc)[:120])


async def read_tool_windows(job_id: str, *, limit: int = TOOL_WINDOW_MAX_ENTRIES) -> list[dict[str, Any]]:
    """读回该任务的工具窗口快照（最新在前）；读不到返回空列表，不抛错。"""
    target = str(job_id or "")
    if not target:
        return []
    try:
        from app.core.redis import get_redis

        rows = await get_redis().lrange(tool_window_key(target), 0, -1)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[tool-window] 诊断快照读取失败 job={}: {}", target[:12], str(exc)[:120])
        return []
    out: list[dict[str, Any]] = []
    for raw in rows or []:
        try:
            item = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(item, dict):
            out.append(item)
    capped = max(1, int(limit or TOOL_WINDOW_MAX_ENTRIES))
    return list(reversed(out))[:capped]



def _frame_seq(frame: Any) -> int:
    if not isinstance(frame, dict):
        return 0
    try:
        return int(frame.get("seq") or 0)
    except (TypeError, ValueError):
        return 0


async def build_gap_recovery(
    job_id: str,
    *,
    after_seq: int = 0,
    limit: int = DEFAULT_RESUME_LIMIT,
    include_snapshot: bool = True,
) -> dict[str, Any]:
    """组装一次断线恢复包（**永远不抛异常**：降级信息写在返回值里）。

    两条硬规则（评审 P0）：

    1. **旧快照不得覆盖新状态**。``snapshot_applicable`` 只在
       ``snapshot_seq >= client_seq`` 时为真；否则前端必须**保留**本地视图，只追加增量
       （快照落后于客户端时套用会把界面回退到几秒前）。
    2. **"读不到日志" ≠ "没有新事件"**。``event_log_available=False`` 时绝不报
       ``truncated=false / retry_after_ms=0``：那会让前端以为追平了，实际上什么都没读到。
       日志被裁剪（``oldest_seq > client_seq + 1``）时给 ``gap_detected=True``。
    """
    from app.services import job_event_log

    target = str(job_id or "")
    capped = max(1, min(int(limit or DEFAULT_RESUME_LIMIT), 1000))
    client_seq = max(0, int(after_seq or 0))

    snapshot_payload: dict[str, Any] | None = None
    snapshot_seq = 0
    if include_snapshot and target:
        try:
            # 唯一入口：快照键与序列化都只属于 job_snapshot_store（见其写入契约）。
            # 恢复路径拿"已收缩载荷"，不碰 JobRunView、也不自己拼键。
            from app.services.job_snapshot_store import read_snapshot_payload

            snapshot_payload, snapshot_seq = await read_snapshot_payload(target)
        except Exception as exc:  # noqa: BLE001 - 快照不可用不影响增量补拉
            logger.debug("[resume] 快照读取失败（降级为纯增量）: {}", str(exc)[:120])
            snapshot_payload, snapshot_seq = None, 0

    baseline = max(client_seq, snapshot_seq)
    events: list[dict[str, Any]] = []
    log_state: dict[str, Any] = {"available": False, "oldest_seq": 0, "head_seq": 0, "count": 0}
    if target:
        try:
            events, log_state = await job_event_log.read_frames_with_state(
                target, after_seq=baseline, limit=capped
            )
        except Exception as exc:  # noqa: BLE001 - 读不到就当"不可用"（不是"没有"）
            logger.debug("[resume] 增量读取失败（降级）: {}", str(exc)[:120])
            events, log_state = [], {"available": False, "oldest_seq": 0, "head_seq": 0, "count": 0}

    log_available = bool(log_state.get("available"))
    oldest_seq = int(log_state.get("oldest_seq") or 0)
    head = int(log_state.get("head_seq") or 0)
    head = max(head, baseline, snapshot_seq, max((_frame_seq(item) for item in events), default=0))
    truncated = len(events) >= capped and head > (_frame_seq(events[-1]) if events else baseline)
    # 缺口：日志最早一条已经**晚于**客户端水位的下一条 → 中间那段被 ltrim 裁掉了。
    gap_detected = bool(log_available and oldest_seq and oldest_seq > client_seq + 1)
    # 快照能不能用：只有"快照不落后于客户端"时才可以覆盖本地视图，否则就是状态回退。
    snapshot_applicable = snapshot_payload is not None and snapshot_seq >= client_seq

    if snapshot_payload is None:
        mode = "events_only" if events else "full_refetch"
    elif not snapshot_applicable:
        # 快照落后：仍然返回（供展示/审计），但明确标记"别用它覆盖"。
        mode = "events_only"
    else:
        mode = "snapshot_delta" if events else "snapshot_only"

    if not log_available and snapshot_payload is None:
        mode = "full_refetch"
    elif not log_available and mode == "snapshot_only":
        # 日志读不到 + 有快照：不能说"已追平"，让客户端稍后重试增量。
        mode = "snapshot_delta"

    retry_after_ms = 0
    if truncated or not log_available or gap_detected:
        retry_after_ms = RETRY_AFTER_MS
    if mode == "full_refetch":
        retry_after_ms = max(retry_after_ms, RETRY_AFTER_MS)

    return {
        "job_id": target,
        "resume_mode": mode,
        "snapshot": snapshot_payload,
        # 只有 True 才允许用 snapshot 覆盖本地视图（防状态回退）。
        "snapshot_applicable": bool(snapshot_applicable),
        "baseline_seq": int(baseline),
        "client_seq": int(client_seq),
        "snapshot_seq": int(snapshot_seq),
        "head_seq": int(head),
        "oldest_seq": int(oldest_seq),
        "last_seq": _frame_seq(events[-1]) if events else int(baseline),
        "count": len(events),
        "truncated": bool(truncated),
        # 日志可读性：False 时"没有事件"没有任何结论意义（读失败也是空数组）。
        "event_log_available": bool(log_available),
        "event_log_count": int(log_state.get("count") or 0),
        # 明确缺口：客户端水位与日志起点之间的事件已经不存在了。
        "gap_detected": bool(gap_detected),
        "events": events,
        # 客户端据此决定"立刻再拉一次"还是"等一会儿"：截断/读失败/有缺口都要等；
        # 三者都不是时才算追平（0 = 别再轮询）。
        "retry_after_ms": retry_after_ms,
        "protocol": "canonical" if any(isinstance(item.get("payload"), dict) for item in events) else "",
        "message": _MESSAGES[mode] if log_available else _DEGRADED_MESSAGE,
    }


_MESSAGES: dict[str, str] = {
    "snapshot_delta": "已返回快照 + 增量事件",
    "snapshot_only": "已返回快照（无新事件）",
    "events_only": "快照不可用或落后于你的水位，已返回增量事件（请保留本地视图）",
    "full_refetch": "快照与事件日志都不可用，请以任务详情接口恢复",
}

#: 日志读失败时的统一文案：**不能**说成"没有新事件"。
_DEGRADED_MESSAGE = "事件日志暂不可读，无法确认是否已追平；请稍后重试或改用任务详情接口"


__all__ = [
    "DEFAULT_RESUME_LIMIT",
    "RETRY_AFTER_MS",
    "TOOL_WINDOW_KEY_PREFIX",
    "TOOL_WINDOW_MAX_ENTRIES",
    "TOOL_WINDOW_TTL_SECONDS",
    "build_gap_recovery",
    "read_tool_windows",
    "record_tool_window",
    "tool_window_key",
]
