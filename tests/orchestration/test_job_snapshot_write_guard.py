"""阶段 3 守卫：Job 快照的 Redis 写入**只能**走 ``job_snapshot_store`` → ``to_snapshot()``。

守卫分两层：

1. **静态扫描**（本文件）：``app/`` 与内核包里出现"绕过唯一写入路径"的代码即失败——
   直接序列化 ``JobRunView``（``model_dump`` / ``model_dump_json``）、使用仅供度量/排障的
   ``to_snapshot_unbounded()``、或在别处拼运行视图快照的 Redis 键；
2. **行为测试**：写入路径必须真的调用 ``to_snapshot()``；把 ``to_snapshot`` 换成抛错，
   写入必须失败（证明没有内部旁路），且生产 ``RedisStateStore`` 只经由该模块写快照。
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest

import app.core.redis as redis_module
from app.agents.orchestration.models import Job, JobStatus
from app.agents.orchestration.runtime.state import RedisStateStore
from app.services import job_snapshot_store
from lumi_contracts import JobRunView, RunState

from _paths import REPO_ROOT
REPO_ROOT = REPO_ROOT

#: 扫描范围：应用层 + 内核包（测试与文档不在守卫范围内）。
SCAN_ROOTS: tuple[Path, ...] = (
    REPO_ROOT / "app",
    REPO_ROOT / "packages" / "contracts" / "src",
    REPO_ROOT / "packages" / "orchestration" / "src",
    REPO_ROOT / "packages" / "execution" / "src",
)

#: 允许的例外：契约自身定义快照形状；快照仓库本身就是唯一写入路径。
ALLOWED_FILES: frozenset[str] = frozenset(
    {
        "app/services/job_snapshot_store.py",
        "packages/contracts/src/lumi_contracts/persistence/run_view.py",
    }
)

SERIALISER_ATTRS: frozenset[str] = frozenset({"model_dump", "model_dump_json"})

#: 产出 ``JobRunView`` 的调用（构造/投影/反序列化）。
VIEW_PRODUCERS: frozenset[str] = frozenset(
    {"JobRunView", "to_job_run_view", "view_from_snapshot", "view_from_job"}
)


class _FakeRedis:
    """最小 Redis 替身：set/get + 任务索引用到的方法。"""

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


def _relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _scan_snapshot_bypass(root: Path) -> list[str]:
    """扫描"绕过唯一写入路径"的代码，返回违规描述列表（空 = 通过）。"""
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        relative = _relative(path)
        if relative in ALLOWED_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        # 规则 B：运行视图快照的 Redis 键只允许在 job_snapshot_store 里拼。
        if "multiagent:job_snapshot:" in text or "JOB_SNAPSHOT_KEY_PREFIX" in text:
            violations.append(f"{relative}: 自行拼装运行视图快照的 Redis 键（第二条写入路径）")
        if "JobRunView" not in text:
            continue
        # 规则 A：只允许 to_snapshot()；度量用的无界快照不得出现在写入方。
        if "to_snapshot_unbounded(" in text:
            violations.append(f"{relative}: 直接使用 to_snapshot_unbounded()（绕过体积收缩）")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        # 规则 C：把"产出 JobRunView 的变量"直接 model_dump 的写法视为绕过 to_snapshot()。
        view_names: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            call = node.value
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            produced = (
                (isinstance(func, ast.Name) and func.id in VIEW_PRODUCERS)
                or (
                    isinstance(func, ast.Attribute)
                    and func.attr in {"model_validate", "model_validate_json"}
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "JobRunView"
                )
            )
            if not produced:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    view_names.add(target.id)
        if not view_names:
            continue
        dumps: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in SERIALISER_ATTRS:
                continue
            receiver = func.value
            if isinstance(receiver, ast.Name) and receiver.id in view_names:
                dumps.append(f"{receiver.id}.{func.attr}")
        if dumps:
            violations.append(f"{relative}: 直接序列化 JobRunView 快照 {sorted(dumps)}（绕过 to_snapshot）")
    return violations


# ── 静态守卫 ───────────────────────────────────────────────


def test_no_module_serialises_a_job_snapshot_bypassing_to_snapshot():
    violations: list[str] = []
    for root in SCAN_ROOTS:
        if root.exists():
            violations.extend(_scan_snapshot_bypass(root))
    assert violations == [], "运行视图快照必须经 job_snapshot_store → to_snapshot() 写入：\n" + "\n".join(violations)


def test_guard_detects_a_planted_bypass(tmp_path):
    """守卫本身必须有效：种一个绕过用例，扫描器要能报出来。"""
    (tmp_path / "bad_snapshot_writer.py").write_text(
        "from lumi_contracts import JobRunView\n"
        "def dump(view):\n"
        "    fresh = JobRunView(job_id='x')\n"
        "    return fresh.model_dump_json()\n",
        encoding="utf-8",
    )
    (tmp_path / "bad_unbounded.py").write_text(
        "from lumi_contracts import JobRunView\n"
        "def raw(view: JobRunView):\n"
        "    return view.to_snapshot_unbounded()\n",
        encoding="utf-8",
    )
    (tmp_path / "bad_key.py").write_text(
        "KEY = 'multiagent:job_snapshot:{}'\n",
        encoding="utf-8",
    )
    violations = _scan_snapshot_bypass(tmp_path)
    blob = "\n".join(violations)
    assert "bad_snapshot_writer.py" in blob
    assert "to_snapshot_unbounded()" in blob
    assert "bad_key.py" in blob


def test_only_one_module_knows_the_snapshot_key():
    key_users = []
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if "multiagent:job_snapshot:" in path.read_text(encoding="utf-8"):
                key_users.append(_relative(path))
    assert key_users == ["app/services/job_snapshot_store.py"]


# ── 行为守卫 ───────────────────────────────────────────────


def _spy_snapshot(monkeypatch) -> list[str]:
    seen: list[str] = []
    original = JobRunView.to_snapshot

    def _spy(self):
        seen.append(str(self.job_id))
        return original(self)

    monkeypatch.setattr(JobRunView, "to_snapshot", _spy)
    return seen


def test_write_snapshot_goes_through_to_snapshot(monkeypatch):
    view = JobRunView(job_id="job-guard-1", status=RunState.RUNNING)
    original = JobRunView.to_snapshot
    expected = original(view)
    seen = _spy_snapshot(monkeypatch)
    redis = _FakeRedis()

    payload = asyncio.run(job_snapshot_store.write_snapshot(view, ttl_seconds=60, redis=redis))

    assert seen == ["job-guard-1"], "写入必须经过 to_snapshot()（唯一序列化点）"
    stored = redis.values[job_snapshot_store.snapshot_key("job-guard-1")]
    assert redis.ttls[job_snapshot_store.snapshot_key("job-guard-1")] == 60
    assert json.loads(stored) == expected
    assert json.loads(payload) == expected


def test_write_snapshot_has_no_internal_bypass(monkeypatch):
    """把唯一入口换成抛错：写入必须失败，证明内部没有"偷偷 model_dump"的旁路。"""

    def _boom(self):
        raise AssertionError("to_snapshot 被绕过")

    monkeypatch.setattr(JobRunView, "to_snapshot", _boom)
    view = JobRunView(job_id="job-guard-2")
    with pytest.raises(AssertionError):
        asyncio.run(job_snapshot_store.write_snapshot(view, ttl_seconds=60, redis=_FakeRedis()))


def test_persist_job_snapshot_uses_the_single_path(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setattr(redis_module, "get_redis", lambda: redis)
    seen = _spy_snapshot(monkeypatch)
    job = Job(
        job_id="job-guard-7",
        user_id="u1",
        request="x",
        status=JobStatus.RUNNING,
        routing={"execution_state": "waiting_run"},
    )
    view = asyncio.run(job_snapshot_store.persist_job_snapshot(job, ttl_seconds=42, redis=redis))
    assert seen == ["job-guard-7"], "Job → 运行视图快照同样只能经 to_snapshot()"
    assert view.job_id == "job-guard-7"
    key = job_snapshot_store.snapshot_key("job-guard-7")
    assert redis.ttls[key] == 42
    assert json.loads(redis.values[key])["job_id"] == "job-guard-7"


def test_read_snapshot_is_round_trip_and_tolerant():
    view = JobRunView(job_id="job-guard-3", status=RunState.COMPLETED).note_seq(7)
    redis = _FakeRedis()
    asyncio.run(job_snapshot_store.write_snapshot(view, ttl_seconds=60, redis=redis))
    loaded = asyncio.run(job_snapshot_store.read_snapshot("job-guard-3", redis=redis))
    assert loaded is not None
    assert loaded.job_id == "job-guard-3" and loaded.last_seq == 7
    assert asyncio.run(job_snapshot_store.read_snapshot("missing", redis=redis)) is None
    redis.values[job_snapshot_store.snapshot_key("broken")] = "{not json"
    assert asyncio.run(job_snapshot_store.read_snapshot("broken", redis=redis)) is None


def test_production_redis_write_is_wrapped_and_called_from_save_job(monkeypatch):
    """生产 Redis 写入（RedisStateStore.create_job/save_run_view）只经由快照模块。"""
    import app.agents.orchestration.runtime.state as state_module

    redis = _FakeRedis()
    monkeypatch.setattr(redis_module, "get_redis", lambda: redis)
    monkeypatch.setattr(state_module, "get_redis", lambda: redis)
    seen = _spy_snapshot(monkeypatch)

    store = RedisStateStore(ttl_seconds=60)
    view = JobRunView(job_id="job-guard-4", status=RunState.RUNNING)
    asyncio.run(store.save_run_view(view))
    assert seen == ["job-guard-4"]
    assert job_snapshot_store.snapshot_key("job-guard-4") in redis.values
    loaded = asyncio.run(store.get_run_view("job-guard-4"))
    assert loaded is not None and loaded.job_id == "job-guard-4"

    job = Job(job_id="job-guard-5", user_id="u1", request="x", status=JobStatus.RUNNING)
    asyncio.run(store.create_job(job))
    assert seen[-1] == "job-guard-5"
    assert job_snapshot_store.snapshot_key("job-guard-5") in redis.values


def test_snapshot_failure_never_breaks_job_state(monkeypatch):
    """快照写入失败（Redis 抖动）不能让任务状态写入失败。"""
    import app.agents.orchestration.runtime.state as state_module

    class _BrokenStore(RedisStateStore):
        async def save_run_view(self, view):  # pragma: no cover - 直接抛错
            raise RuntimeError("redis down")

    redis = _FakeRedis()
    monkeypatch.setattr(redis_module, "get_redis", lambda: redis)
    monkeypatch.setattr(state_module, "get_redis", lambda: redis)
    store = _BrokenStore(ttl_seconds=60)
    job = Job(job_id="job-guard-6", user_id="u1", request="x", status=JobStatus.RUNNING)
    asyncio.run(store.create_job(job))
    assert redis.values  # 任务状态照旧写成功
