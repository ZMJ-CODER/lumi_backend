"""Job 运行视图快照的 Redis **唯一写入路径**（阶段 3 硬规则）。

问题：进程/轮询/恢复都会拿"Job 快照"去序列化落库，谁都能 ``model_dump()`` 一把
写进 Redis——绕开体积收缩（``SNAPSHOT_MAX_BYTES``）、绕开过程日志窗口与归档引用，
快照迟早膨胀到把任务状态写坏。

做法：

* 契约侧 ``JobRunView.to_snapshot()`` 是**唯一的可写快照形状**（内部先量体积、超限
  强制收缩并告警）；
* 本模块的 :class:`SnapshotWriter` 是**唯一调用它的地方**：序列化（:func:`snapshot_payload`
  / :func:`snapshot_json`）与 Redis 写入都在这里；生产写入方
  （``RedisStateStore``）只经由它写运行视图快照，见
  ``app/agents/orchestration/state.py::save_run_view``；
* ``tests/test_job_snapshot_write_guard.py`` 静态扫描"绕过本模块直接序列化
  ``JobRunView``"的代码，发现即失败。

**写入契约（不是开关）**::

    FLAG DOES NOT CONTROL SNAPSHOT WRITES

运行视图快照是任务恢复与前端刷新的**基础设施**：只要调用方要求保存任务状态
（``create_job`` / ``save_job`` / ``save_run_view``），快照就必须写入，任何特性开关
（``ARCHIVE_CONTENT_V2`` / ``EFFECT_JOURNAL_RECOVERY_V2`` / ``INTEGRATION_SHADOW_MODE``
…）都不得关闭或跳过它——本模块**不读取任何特性开关**（读取路径 :func:`read_snapshot`
同样与开关无关）。开关允许影响的是"视图里放什么"（例如 ``ARCHIVE_CONTENT_V2`` 决定
过程日志归档是否产出、因而决定快照里有没有归档引用），而不是"快照是否落库"。
这条边界由 ``tests/test_snapshot_write_contract.py`` 断言（文档 vs 行为）。

每次写入都会记录 TTL、写入口版本（:data:`SNAPSHOT_WRITER_VERSION`）、快照体积，并打
一条结构化日志 + 指标 ``lumi_job_snapshot_writes_total``，便于回答"快照到底写没写、
按什么 TTL 写的、写的是哪一版"。

键空间：``multiagent:job_snapshot:{job_id}``，与任务状态键（``multiagent:job:{job_id}``）
分离——状态键仍是内核 ``Job`` 的权威快照，本键是本阶段新增的**运行视图快照**。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from loguru import logger

from lumi_contracts import JobRunView

#: 运行视图快照的 Redis 键前缀（除本模块外任何地方出现该字面量都属于绕开写入路径）。
JOB_SNAPSHOT_KEY_PREFIX = "multiagent:job_snapshot:"

#: 写入侧契约原文（文档与行为必须一致；见模块 docstring 与 ``.env.example``）。
SNAPSHOT_WRITE_CONTRACT = "FLAG DOES NOT CONTROL SNAPSHOT WRITES"

#: 写入口版本：与契约 ``JobRunView.version``（数据版本）分开，表示"写入器/编码方式"版本。
SNAPSHOT_WRITER_VERSION = 1


def snapshot_key(job_id: str) -> str:
    return f"{JOB_SNAPSHOT_KEY_PREFIX}{job_id}"


def snapshot_payload(view: JobRunView) -> dict[str, Any]:
    """唯一的快照序列化点：只允许 ``to_snapshot()``（含体积收缩）。"""
    return view.to_snapshot()


def snapshot_json(view: JobRunView) -> str:
    """快照 → 可写 Redis 的 JSON 文本（键序稳定，便于对拍/排障）。"""
    return json.dumps(
        snapshot_payload(view),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def view_from_snapshot(payload: Any) -> JobRunView:
    """快照载荷 → 契约 ``JobRunView``（读取路径同样只认这一种形状）。"""
    if isinstance(payload, str):
        return JobRunView.model_validate_json(payload)
    return JobRunView.model_validate(payload)


def view_from_job(
    job: Any,
    *,
    conversation_id: str = "",
    routing: dict[str, Any] | None = None,
) -> JobRunView:
    """内核 ``Job`` → 契约 ``JobRunView``（含过程日志与归档引用，均只放引用/摘要）。"""
    from lumi_orch.run_view import run_view

    from app.contracts.run_view import to_job_run_view

    raw = run_view(job) if hasattr(job, "job_id") else {}
    view = to_job_run_view(
        raw,
        conversation_id=str(conversation_id or getattr(job, "conversation_id", "") or ""),
        routing=dict(routing if routing is not None else (getattr(job, "routing", {}) or {})),
    )
    process_log = list(getattr(job, "process_log", None) or [])
    if process_log:
        view = view.with_process_log(process_log)
    from app.services.process_log_archive import archive_metadata

    meta = archive_metadata(job)
    if meta is not None:
        view = view.with_log_archive(str(meta.get("ref") or ""), count=int(meta.get("count") or 0))
    return view


def default_ttl_seconds() -> int:
    """快照 TTL（秒）：``AGENT_JOBS_TTL_SECONDS``，配置不可用时退回 1 天。"""
    try:
        from app.core.config import settings

        return max(1, int(settings.AGENT_JOBS_TTL_SECONDS))
    except Exception:  # noqa: BLE001 - 配置不可用时退回一天，绝不能变成永久键
        return 86400


def _ttl_seconds(explicit: int | None) -> int:
    if explicit is not None:
        return max(1, int(explicit))
    return default_ttl_seconds()


@dataclass(frozen=True, slots=True)
class SnapshotWriteResult:
    """一次快照写入的可审计结果（TTL / 版本 / 体积 / 键）。"""

    key: str
    job_id: str
    ttl_seconds: int
    #: 写入口版本（:data:`SNAPSHOT_WRITER_VERSION`）。
    version: int
    #: 契约数据版本（``JobRunView.version``）。
    view_version: int
    bytes_written: int
    payload: str
    contract: str = SNAPSHOT_WRITE_CONTRACT


class SnapshotWriter:
    """运行视图快照的**唯一写入器**：序列化只经 ``to_snapshot()``。

    ``FLAG DOES NOT CONTROL SNAPSHOT WRITES``：本类不读取任何特性开关，``write()`` 被
    调用就一定会写（Redis 故障由调用方按"不影响任务状态"处理）。
    """

    __slots__ = ("_ttl", "_redis")

    def __init__(self, ttl_seconds: int | None = None, *, redis: Any = None) -> None:
        self._ttl = _ttl_seconds(ttl_seconds)
        self._redis = redis

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    @property
    def version(self) -> int:
        return SNAPSHOT_WRITER_VERSION

    async def write(self, view: JobRunView, *, redis: Any = None) -> SnapshotWriteResult:
        """写入快照并记录 TTL/版本/体积（返回可审计结果）。"""
        from app.core.redis import get_redis

        payload = snapshot_json(view)
        key = snapshot_key(str(view.job_id))
        client = redis if redis is not None else (self._redis if self._redis is not None else get_redis())
        await client.set(key, payload, ex=self._ttl)
        result = SnapshotWriteResult(
            key=key,
            job_id=str(view.job_id),
            ttl_seconds=self._ttl,
            version=SNAPSHOT_WRITER_VERSION,
            view_version=int(getattr(view, "version", 1) or 1),
            bytes_written=len(payload.encode("utf-8")),
            payload=payload,
        )
        _record_write(result)
        return result


def _record_write(result: SnapshotWriteResult) -> None:
    """写入日志 + 指标（每次写入都必须能回答"TTL 多少、哪一版、多大"）。"""
    logger.debug(
        "[job-snapshot] 写入 key={} ttl={}s version={} view_version={} bytes={} contract={}",
        result.key,
        result.ttl_seconds,
        result.version,
        result.view_version,
        result.bytes_written,
        result.contract,
    )
    try:
        from app.core.observability import inc_job_snapshot_write

        inc_job_snapshot_write(status="written", version=result.version)
    except Exception as exc:  # noqa: BLE001 - 指标失败绝不影响写入路径
        logger.debug("[job-snapshot] 写入指标记录失败: {}", str(exc)[:120])


def snapshot_writer(*, ttl_seconds: int | None = None, redis: Any = None) -> SnapshotWriter:
    """构造写入器（``RedisStateStore`` 用它持有**单例**写入器）。"""
    return SnapshotWriter(ttl_seconds, redis=redis)


async def write_snapshot(
    view: JobRunView,
    *,
    ttl_seconds: int | None = None,
    redis: Any = None,
) -> str:
    """把运行视图快照写进 Redis（兼容入口），返回写入的 JSON。

    生产路径请复用 :class:`SnapshotWriter` 单例（``RedisStateStore.save_run_view``）；
    这个函数只是给"一次性写入"的调用方/测试用的同一实现。
    """
    writer = SnapshotWriter(ttl_seconds, redis=redis)
    return (await writer.write(view)).payload


async def read_snapshot(job_id: str, *, redis: Any = None) -> JobRunView | None:
    """读回运行视图快照（键不存在/内容损坏时返回 ``None``，不抛错）。

    读取**不受任何特性开关控制**（``FLAG DOES NOT CONTROL SNAPSHOT WRITES`` 的边界：
    开关可以改变快照内容，但不能让读取路径"看不见"已写入的快照）。
    """
    from app.core.redis import get_redis

    client = redis if redis is not None else get_redis()
    raw = await client.get(snapshot_key(str(job_id)))
    if not raw:
        return None
    try:
        return view_from_snapshot(raw)
    except Exception as exc:  # noqa: BLE001 - 快照损坏按"没有快照"处理
        logger.warning("[job-snapshot] 快照反序列化失败 job={} err={}", str(job_id)[:12], str(exc)[:120])
        return None


async def read_snapshot_baseline(job_id: str, *, redis: Any = None) -> tuple[JobRunView | None, int]:
    """快照 + 它覆盖到的**事件水位**（``last_seq``）。

    断线恢复（``app/services/resume_snapshot.py``）只认这一个入口：快照天然是一次
    "某个 seq 上的检查点"，恢复路径因此不需要重建视图，也**不需要自己知道快照键**
    （键只在本模块出现这条契约由 ``tests/test_job_snapshot_write_guard.py`` 守着——
    多一个模块拼同一个键，就多一条可能绕过体积收缩与写入规范的路径）。
    """
    view = await read_snapshot(job_id, redis=redis)
    if view is None:
        return None, 0
    return view, int(getattr(view, "last_seq", 0) or 0)


async def read_snapshot_payload(job_id: str, *, redis: Any = None) -> tuple[dict[str, Any] | None, int]:
    """快照的**已收缩载荷** + 事件水位（给恢复路径用，调用方不需要 ``JobRunView``）。

    为什么单独给一个"返回 dict"的入口：序列化只允许发生在本模块（``snapshot_payload``
    → ``to_snapshot()``）。恢复路径若自己调 ``view.to_snapshot()``，静态守卫会（正确地）
    把它认成"第二条序列化路径"——即使当前参数是安全的，也不能靠"这次没写错"来维持边界。
    """
    view = await read_snapshot(job_id, redis=redis)
    if view is None:
        return None, 0
    return snapshot_payload(view), int(getattr(view, "last_seq", 0) or 0)


async def persist_job_snapshot(job: Any, *, ttl_seconds: int | None = None, redis: Any = None) -> JobRunView:
    """内核 ``Job`` → 运行视图快照 → Redis（调用方无需再自己序列化）。"""
    view = view_from_job(job)
    await SnapshotWriter(ttl_seconds, redis=redis).write(view)
    return view


__all__ = [
    "JOB_SNAPSHOT_KEY_PREFIX",
    "SNAPSHOT_WRITE_CONTRACT",
    "SNAPSHOT_WRITER_VERSION",
    "SnapshotWriteResult",
    "SnapshotWriter",
    "default_ttl_seconds",
    "persist_job_snapshot",
    "read_snapshot",
    "read_snapshot_baseline",
    "read_snapshot_payload",
    "snapshot_json",
    "snapshot_key",
    "snapshot_payload",
    "snapshot_writer",
    "view_from_job",
    "view_from_snapshot",
    "write_snapshot",
]
