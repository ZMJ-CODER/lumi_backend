"""Job 快照写入语义的**契约测试**：``FLAG DOES NOT CONTROL SNAPSHOT WRITES``。

决策（已与用户确认）：快照是**持久化基础设施**，始终写入；特性开关**不得**被假定为
控制快照写入。本文件把该决策变成可执行断言：

* 无论开关全关 / 全开，快照都必须写入（写入路径不读开关）；
* 只有 ``SnapshotWriter`` 一条写入路径，且只经 ``JobRunView.to_snapshot()``；
* 每次写入都记录 TTL、写入口版本（``SNAPSHOT_WRITER_VERSION``）、快照数据版本与体积
  （日志 + 指标 ``lumi_job_snapshot_writes_total``）；
* 文档 vs 行为：docstring / ``.env.example`` 写着"始终写入"，代码就必须"始终写入"，
  且不存在名为 ``*SNAPSHOT*`` 的开关；若将来有开关读快照，必须在此显式登记边界。
"""

from __future__ import annotations

import asyncio
import ast
import json
from pathlib import Path

import pytest
from loguru import logger

import app.core.redis as redis_module
from app.agents.orchestration.models import Job, JobStatus
from app.agents.orchestration.runtime.state import RedisStateStore
from app.platform.runtime import feature_flags
from app.core.config import settings
from app.services import job_snapshot_store
from lumi_contracts import JobRunView, RunState

from _paths import REPO_ROOT
REPO_ROOT = REPO_ROOT

#: 契约原文（模块、state、``.env.example`` 必须逐字一致）。
CONTRACT_TEXT = "FLAG DOES NOT CONTROL SNAPSHOT WRITES"

#: 已知会**读取**快照的开关：空集 = 读取路径同样不受开关控制。
#: 若将来有开关要读快照，必须在这里登记并在文档里写明边界，否则本测试失败。
FLAGS_THAT_READ_SNAPSHOTS: frozenset[str] = frozenset()


class _FakeRedis:
    """最小 Redis 替身（set/get + 任务索引）。"""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}
        self.lists: dict[str, list[str]] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.values[key] = value
        self.ttls[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def lrem(self, key: str, count: int, value: str) -> int:
        rows = self.lists.get(key, [])
        self.lists[key] = [row for row in rows if row != value]
        return len(rows) - len(self.lists[key])

    async def lpush(self, key: str, value: str) -> int:
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    async def ltrim(self, key: str, start: int, end: int) -> bool:
        rows = self.lists.get(key, [])
        self.lists[key] = rows[start:] if end == -1 else rows[start : end + 1]
        return True

    async def expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = seconds
        return True


def _set_all_flags(monkeypatch, value: bool) -> None:
    for flag in feature_flags.ALL_FLAGS:
        monkeypatch.setattr(settings, flag, value, raising=False)


def _capture_logs():
    records: list[str] = []
    sink_id = logger.add(lambda message: records.append(message), level="DEBUG")
    return records, sink_id


# ── 文档 vs 行为 ───────────────────────────────────────────


def test_contract_text_is_published_in_module_state_and_env_example():
    assert job_snapshot_store.SNAPSHOT_WRITE_CONTRACT == CONTRACT_TEXT
    module_source = Path(job_snapshot_store.__file__).read_text(encoding="utf-8")
    assert CONTRACT_TEXT in module_source
    state_source = (REPO_ROOT / "app" / "agents" / "orchestration" / "runtime" / "state.py").read_text(encoding="utf-8")
    assert CONTRACT_TEXT in state_source
    env_source = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert CONTRACT_TEXT in env_source
    # 文档里必须写明 TTL 的来源与"写入始终发生"。
    assert "AGENT_JOBS_TTL_SECONDS" in env_source.split(CONTRACT_TEXT, 1)[1]


def test_write_path_never_reads_a_feature_flag():
    """行为与文档一致：写入路径不引用任何开关（AST 级扫描，连名字都不许出现）。"""
    source = Path(job_snapshot_store.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)
    assert "feature_enabled" not in referenced
    assert "flag_snapshot" not in referenced
    # 连开关名字都不许出现在可执行标识符里（docstring 里提及不算"读取开关"）。
    assert not (referenced & set(feature_flags.FEATURE_FLAGS))


def test_no_feature_flag_is_named_after_snapshots_and_readers_are_registered():
    assert FLAGS_THAT_READ_SNAPSHOTS == frozenset()
    assert not [flag for flag in feature_flags.ALL_FLAGS if "SNAPSHOT" in flag.upper()]
    # 读取路径（read_snapshot）同样不读开关：文档声明的边界成立。
    read_source = Path(job_snapshot_store.__file__).read_text(encoding="utf-8")
    read_body = read_source.split("async def read_snapshot", 1)[1]
    assert "feature_enabled" not in read_body


# ── 开关不控制写入 ─────────────────────────────────────────


@pytest.mark.parametrize("flag_value", [False, True])
def test_snapshot_is_written_regardless_of_flags(monkeypatch, flag_value):
    _set_all_flags(monkeypatch, flag_value)
    redis = _FakeRedis()
    view = JobRunView(job_id=f"job-flag-{int(flag_value)}", status=RunState.RUNNING)

    result = asyncio.run(job_snapshot_store.SnapshotWriter(ttl_seconds=60).write(view, redis=redis))

    key = job_snapshot_store.snapshot_key(view.job_id)
    assert redis.values[key] and redis.ttls[key] == 60
    assert result.contract == CONTRACT_TEXT
    assert json.loads(redis.values[key])["job_id"] == view.job_id


def test_redis_state_store_writes_snapshot_with_every_flag_off(monkeypatch):
    """生产入口（RedisStateStore）在开关全关时同样写快照——快照不是灰度功能。"""
    import app.agents.orchestration.runtime.state as state_module

    _set_all_flags(monkeypatch, False)
    redis = _FakeRedis()
    monkeypatch.setattr(redis_module, "get_redis", lambda: redis)
    monkeypatch.setattr(state_module, "get_redis", lambda: redis)
    store = RedisStateStore(ttl_seconds=30)
    job = Job(job_id="job-off", user_id="u1", request="x", status=JobStatus.RUNNING)

    asyncio.run(store.create_job(job))

    assert job_snapshot_store.snapshot_key("job-off") in redis.values
    assert redis.ttls[job_snapshot_store.snapshot_key("job-off")] == 30


# ── 单一写入路径 ───────────────────────────────────────────


def test_state_store_holds_a_single_snapshot_writer(monkeypatch):
    store = RedisStateStore(ttl_seconds=45)
    first = store.snapshot_writer()
    assert first is store.snapshot_writer(), "同一个 store 只允许一个写入器实例"
    assert first.ttl_seconds == 45
    assert isinstance(first, job_snapshot_store.SnapshotWriter)


def test_write_is_the_only_path_and_goes_through_to_snapshot(monkeypatch):
    seen: list[str] = []
    original = JobRunView.to_snapshot

    def _spy(self):
        seen.append(str(self.job_id))
        return original(self)

    monkeypatch.setattr(JobRunView, "to_snapshot", _spy)
    view = JobRunView(job_id="job-single", status=RunState.RUNNING)
    expected = original(view)

    payload = asyncio.run(job_snapshot_store.write_snapshot(view, ttl_seconds=60, redis=_FakeRedis()))

    assert seen == ["job-single"]
    assert json.loads(payload) == expected


def test_snapshot_writer_has_no_internal_bypass(monkeypatch):
    def _boom(self):
        raise AssertionError("to_snapshot 被绕过")

    monkeypatch.setattr(JobRunView, "to_snapshot", _boom)
    with pytest.raises(AssertionError):
        asyncio.run(
            job_snapshot_store.SnapshotWriter(ttl_seconds=60).write(
                JobRunView(job_id="job-bypass"), redis=_FakeRedis()
            )
        )


# ── TTL / 版本 / 指标 / 日志都要被记录 ─────────────────────


def test_write_records_ttl_version_and_size(monkeypatch):
    redis = _FakeRedis()
    view = JobRunView(job_id="job-meta", status=RunState.RUNNING).note_seq(3)
    result = asyncio.run(job_snapshot_store.SnapshotWriter(ttl_seconds=42).write(view, redis=redis))

    assert (result.ttl_seconds, result.version) == (42, job_snapshot_store.SNAPSHOT_WRITER_VERSION)
    assert result.view_version == int(view.version)
    assert result.bytes_written == len(result.payload.encode("utf-8")) > 0
    assert result.key == job_snapshot_store.snapshot_key("job-meta")
    assert result.contract == CONTRACT_TEXT


def test_write_emits_a_metric(monkeypatch):
    calls: list[tuple[str, int]] = []

    def _spy(*, status: str = "written", version: int = 0) -> None:
        calls.append((status, int(version)))

    monkeypatch.setattr("app.observability.observability.inc_job_snapshot_write", _spy)
    asyncio.run(
        job_snapshot_store.SnapshotWriter(ttl_seconds=10).write(
            JobRunView(job_id="job-metric"), redis=_FakeRedis()
        )
    )

    assert calls == [("written", job_snapshot_store.SNAPSHOT_WRITER_VERSION)]


def test_write_logs_ttl_and_version():
    records, sink_id = _capture_logs()
    try:
        asyncio.run(
            job_snapshot_store.SnapshotWriter(ttl_seconds=77).write(
                JobRunView(job_id="job-log"), redis=_FakeRedis()
            )
        )
    finally:
        logger.remove(sink_id)

    line = next(text for text in records if "job-snapshot" in text and "写入" in text)
    assert "ttl=77s" in line
    assert f"version={job_snapshot_store.SNAPSHOT_WRITER_VERSION}" in line
    assert "bytes=" in line and CONTRACT_TEXT in line


def test_default_ttl_comes_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_JOBS_TTL_SECONDS", 1234)
    assert job_snapshot_store.default_ttl_seconds() == 1234
    assert job_snapshot_store.SnapshotWriter().ttl_seconds == 1234


def test_read_snapshot_is_flag_independent(monkeypatch):
    """边界：开关可以让快照"少点内容"，但不能让读取路径"看不见"已写入的快照。"""
    _set_all_flags(monkeypatch, True)
    redis = _FakeRedis()
    asyncio.run(
        job_snapshot_store.SnapshotWriter(ttl_seconds=60).write(
            JobRunView(job_id="job-read", status=RunState.COMPLETED), redis=redis
        )
    )
    loaded = asyncio.run(job_snapshot_store.read_snapshot("job-read", redis=redis))
    assert loaded is not None and loaded.job_id == "job-read"
