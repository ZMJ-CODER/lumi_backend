"""``job_runs`` / ``job_steps`` 异步投影回归（方案 §3.3）。

覆盖验收场景：

* DB 写失败**不阻塞**任务执行（失败只记日志并保留待补写）；
* 幂等：同 ``(job_id, step_id, attempt)`` 重复投影只更新，不产生重复行；
* **旧检查点不覆盖新状态**（``checkpoint_version`` 更小的行直接跳过）；
* 攒批按 ``batch_size`` / ``flush_interval`` 触发，不是"每个事件一次往返"。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

from lumi_contracts.persistence.checkpoint import StepCheckpoint, StepCheckpointState
from app.services.job_projection import JobProjector, rows_from_checkpoint


class _FakeSession:
    """记录 upsert 语句的最小会话替身。"""

    def __init__(self, sink: list[tuple[str, dict[str, Any]]], *, fail: bool = False) -> None:
        self._sink = sink
        self._fail = fail

    async def execute(self, statement) -> None:
        if self._fail:
            raise RuntimeError("db down")
        self._sink.append((str(statement), dict(statement.compile().params if hasattr(statement, "compile") else {})))

    @asynccontextmanager
    async def begin(self):
        yield self


class _FakeFactory:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.rows: list[tuple[str, dict[str, Any]]] = []
        self._fail = fail

    def __call__(self):
        self.calls += 1
        return self

    async def __aenter__(self):
        return _FakeSession(self.rows, fail=self._fail)

    async def __aexit__(self, *_exc) -> bool:
        return False


def _checkpoint(step_id: str, *, version: int, status=StepCheckpointState.COMPLETED) -> StepCheckpoint:
    return StepCheckpoint(
        job_id="job-p1",
        step_id=step_id,
        attempt=1,
        tool_name="workspace_write_file",
        status=status,
        result_ref={"id": f"r-{step_id}", "sha256": "abc"},
        effect_status="confirmed",
        checkpoint_version=version,
    )


def test_rows_from_checkpoint_only_carries_summary_and_reference():
    row = rows_from_checkpoint(_checkpoint("s1", version=3))
    assert row.job_id == "job-p1" and row.step_id == "s1"
    assert row.result_ref == {"id": "r-s1", "sha256": "abc"}
    assert row.checkpoint_version == 3
    assert "body" not in asdict(row)


def test_projection_is_queued_batched_and_idempotent():
    clock = [1000.0]
    factory = _FakeFactory()
    projector = JobProjector(
        batch_size=10,
        flush_interval=5,
        enabled=True,
        session_factory=factory,
        clock=lambda: clock[0],
    )

    projector.queue_step(rows_from_checkpoint(_checkpoint("s1", version=1)))
    assert projector.pending == 1
    # 未到批量阈值且未到时间阈值 → 不 flush（不是每个事件一次 DB 往返）。
    assert asyncio.run(projector.flush()) == 0
    assert factory.calls == 0

    clock[0] += 6  # 时间阈值到达
    assert asyncio.run(projector.flush()) == 1
    assert factory.calls == 1

    for index in range(2, 12):
        projector.queue_step(rows_from_checkpoint(_checkpoint(f"s{index}", version=index)))
    assert projector.should_flush() is True
    written = asyncio.run(projector.flush())
    assert written == 10
    assert factory.calls == 2
    assert projector.pending == 0


def test_older_checkpoint_never_overwrites_newer_state():
    projector = JobProjector(batch_size=1, enabled=True, session_factory=_FakeFactory())
    projector.queue_step(rows_from_checkpoint(_checkpoint("s1", version=5)))
    projector.queue_step(rows_from_checkpoint(_checkpoint("s1", version=2)))
    queued = projector._steps[("job-p1", "s1", 1)]  # noqa: SLF001 - 断言队列内容
    assert queued.checkpoint_version == 5


def test_db_failure_keeps_rows_for_later_and_never_raises():
    projector = JobProjector(batch_size=1, enabled=True, session_factory=_FakeFactory(fail=True))
    projector.queue_step(rows_from_checkpoint(_checkpoint("s1", version=1)))
    assert asyncio.run(projector.flush(force=True)) == 0
    # 失败 → 待补写仍在（恢复后可补写），且没有抛给调用方（不阻塞任务执行）。
    assert projector.pending == 1
    assert projector.stats["failures"] == 1


def test_disabled_projection_is_a_noop():
    from app.services.job_projection import _RunRow

    projector = JobProjector(enabled=False)
    projector.queue_step(rows_from_checkpoint(_checkpoint("s1", version=1)))
    projector.queue_run(_RunRow(job_id="job-p1"))
    assert projector.pending == 0
    assert asyncio.run(projector.flush(force=True)) == 0
