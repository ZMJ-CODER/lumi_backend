"""任务级 deadline：后台 Job 的每一段执行都自带预算（方案 §deadline 的下一步）。

要修的两个方向（都真实存在）：

1. **后台任务继承请求预算** —— Job 的协程是在请求上下文里 ``create_task`` 的，
   contextvars 会被复制。SSE 断开后请求预算可能只剩几秒，长任务会跑到一半被请求预算
   掐死（"任务莫名超时"）；
2. **后台任务完全没有上限** —— 没有请求上下文时 ``get_remaining_budget()`` 是 ``inf``，
   一个卡住的 Provider 调用能把 worker 永久占住。

因此任务预算的语义是**替换 + 显式**：装上任务自己的绝对截止时间并脱离请求作用域；
配置为 0 时仍然 ``clear_deadline()``（"不限制"必须是明确的，而不是继承来的）。
"一段执行"是刻意口径：暂停/等审批不消耗预算，否则审批慢一点就把任务判死。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.platform.runtime import deadline as dl


@pytest.fixture(autouse=True)
def _clean_deadline():
    """每个用例前后都清干净（contextvars 会跨用例泄漏，导致"假通过"）。

    同时**守住模块级时钟**：``dl._clock`` 是模块级函数，任何用例把它改掉而不还原，
    都会污染其它文件里的时间轴对拍（实测打挂了 monotonic 轴断言）。
    """
    original_clock = dl._clock
    dl.clear_deadline()
    yield
    dl._clock = original_clock
    dl.clear_deadline()


@pytest.fixture()
def job_budget(monkeypatch):
    def _set(seconds: float):
        from app.core.config import settings

        monkeypatch.setattr(settings, "JOB_DEADLINE_SECONDS", float(seconds))
        monkeypatch.setattr(settings, "JOB_DEADLINE_MIN_BUDGET_SECONDS", 1.0)

    return _set


# ── 1. 装配：替换请求预算，而不是取更紧 ─────────────────────


def test_job_deadline_detaches_from_a_tighter_request_budget(job_budget):
    """请求预算只剩 2 秒，任务预算 900 秒 → 任务拿到的是 **900**，不是 2。"""
    job_budget(900)
    token = dl.set_deadline(2.0, source="orchestrator.stream")
    assert dl.get_remaining_budget() <= 2.0

    job_token = dl.install_job_deadline()
    try:
        remaining = dl.get_remaining_budget()
        assert remaining > 800, "任务预算必须替换（而不是取 min）请求预算"
        assert dl.is_job_scope() is True
        assert dl.budget_snapshot()["scope"] == "job"
    finally:
        job_token.reset()
    # 还原后回到请求作用域（流式请求串行处理多个任务时必须还原）
    assert dl.is_job_scope() is False
    assert dl.get_remaining_budget() <= 2.0
    token.reset()


def test_disabled_job_budget_is_explicitly_unbounded(job_budget):
    """``JOB_DEADLINE_SECONDS=0`` → 不限制，但**明确 clear**，不继承请求的剩余预算。"""
    job_budget(0)
    dl.set_deadline(5.0, source="orchestrator.stream")
    token = dl.install_job_deadline()
    assert token is None
    assert dl.get_remaining_budget() == float("inf"), "关闭任务预算 = 不限制（也不是继承请求预算）"
    assert dl.get_deadline() is None
    assert dl.is_job_scope() is False
    assert dl.budget_snapshot()["unbounded"] is True
    assert dl.budget_snapshot()["scope"] == "unbounded"


def test_unbounded_without_any_deadline(job_budget):
    job_budget(600)
    assert dl.get_remaining_budget() == float("inf")
    token = dl.install_job_deadline()
    try:
        assert 0 < dl.get_remaining_budget() <= 600
    finally:
        token.reset()


def test_explicit_budget_argument_wins(job_budget):
    """Temporal Activity 传入与自己 ``start_to_close_timeout`` 同源的预算。"""
    job_budget(9999)
    token = dl.install_job_deadline(budget=7.0, source="job.temporal_activity")
    try:
        assert 0 < dl.get_remaining_budget() <= 7.0
        assert dl.budget_snapshot()["source"] == "job.temporal_activity"
    finally:
        token.reset()


def test_job_min_budget_is_scoped(job_budget):
    """任务侧与请求侧的"收尾余量"分开：作用域不同，阈值不同。"""
    from app.core.config import settings

    job_budget(600)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(settings, "REQUEST_DEADLINE_MIN_BUDGET_SECONDS", 30.0)
    try:
        assert dl.effective_min_budget_seconds() == 30.0  # 请求作用域
        dl.set_deadline(60.0, source="orchestrator.stream")
        assert dl.effective_min_budget_seconds() == 30.0
        dl.clear_deadline()
        token = dl.install_job_deadline()
        try:
            # 任务侧是 JOB_DEADLINE_MIN_BUDGET_SECONDS(1.0)，而不是请求侧的 30
            assert dl.effective_min_budget_seconds() == 1.0
            assert dl.has_budget(minimum=5.0) is True
        finally:
            token.reset()
    finally:
        monkey.undo()


def test_budget_snapshot_reports_scope(job_budget):
    job_budget(600)
    assert dl.budget_snapshot()["scope"] == "unbounded"
    request_token = dl.set_deadline(30.0, source="orchestrator.stream")
    assert dl.budget_snapshot()["scope"] == "request"
    job_token = dl.install_job_deadline()
    try:
        snapshot = dl.budget_snapshot()
        assert snapshot["scope"] == "job"
        assert snapshot["unbounded"] is False
        assert snapshot["remaining_seconds"] > 0
    finally:
        job_token.reset()
        request_token.reset()


# ── 2. 每段执行重新锚定：暂停/等审批不消耗任务预算 ────────────


def test_each_execution_segment_re_anchors(job_budget):
    """等审批 40 分钟后恢复执行，仍然拿到完整预算（不是"已经超时"）。"""
    job_budget(60)
    first = dl.install_job_deadline()
    assert dl.get_remaining_budget() > 0
    # 模拟挂起：离开 run() 时还原
    first.reset()

    # 模拟暂停期间的"时间流逝"：把时钟往前挪（不真的 sleep）
    original = dl._clock
    try:
        dl._clock = lambda: original() + 2400  # noqa: SLF001 - 测试里显式操纵时钟
        resumed = dl.install_job_deadline()
        try:
            assert dl.get_remaining_budget() > 50, "恢复后应重新锚定，而不是被判超时"
        finally:
            resumed.reset()
    finally:
        dl._clock = original


def test_inner_scope_can_only_narrow(job_budget):
    """任务内部子路径只能把预算收窄（不能放宽任务预算）。"""
    job_budget(600)
    token = dl.install_job_deadline()
    try:
        before = dl.get_remaining_budget()
        with dl.with_deadline(5.0, source="llm.chat") as scoped:
            assert scoped <= 5.0
            assert dl.request_budget_seconds(cap=100.0) <= 5.0
        assert dl.get_remaining_budget() > before - 1.0
        # cap 大于剩余预算时取剩余预算
        assert dl.request_budget_seconds(cap=10_000) <= before
    finally:
        token.reset()


def test_exhausted_job_budget_raises_deadline_exceeded(job_budget):
    job_budget(600)
    # 用真实的极短预算而不是篡改时钟：时钟是模块级函数，改了就泄漏到其它用例
    # （实测会把"deadline 在 monotonic 轴上"的对拍测试打挂）。
    token = dl.install_job_deadline(budget=0.01)
    try:
        time.sleep(0.05)
        with pytest.raises(dl.DeadlineExceeded) as excinfo:
            dl.ensure_budget(what="llm.chat")
        assert excinfo.value.source.startswith("job")
        assert dl.get_remaining_budget() == 0.0
    finally:
        token.reset()


# ── 3. 执行循环接线：稳定错误码 + 预算确实生效 ────────────────


class _Store:
    def __init__(self, job):
        self.job = job

    async def get_job(self, job_id):
        return self.job

    async def save_job(self, job):
        self.job = job


class _JobErrors:
    def __init__(self):
        self.calls = []

    async def fail(self, job_id, error, *, error_code=None, result=None):
        self.calls.append((job_id, str(error), error_code))
        return None

    async def interrupt(self, job_id, message=""):
        self.calls.append((job_id, message, "INTERRUPTED"))
        return None

    async def ensure_failed(self, job, message):
        self.calls.append((job.job_id, message, "ENSURE_FAILED"))
        return job


def _loop(store, job_errors, execute):
    from app.agents.orchestration.execution.execution_loop_service import ExecutionLoopService
    from app.agents.orchestration.models import Job, JobStatus

    job = Job(job_id="job-1", user_id="u1", request="g")
    job.status = JobStatus.RUNNING
    store.job = job

    class _Tasks:
        def execute(self, job, **kwargs):
            return execute(job, **kwargs)

    class _Finalizer:
        async def suspend_capacity(self, job):
            return None

        async def finalize(self, job):
            return None

    return ExecutionLoopService(
        store=store,
        workers={},
        review=None,
        job_errors=job_errors,
        finalizer=_Finalizer(),
        live_jobs={},
        tasks={},
        api_keys={},
        llm_configs={},
        plan_context={},
        context_getter=lambda _jid: {},
        continue_logical_plan=lambda job: _false(),
        maybe_replan=lambda job, key: _false(),
        node_concurrency=1,
        task_execution_service=_Tasks(),
    )


async def _false():
    return False


@pytest.mark.asyncio
async def test_execution_loop_installs_job_budget(job_budget):
    """执行循环必须**装上**任务预算（并在结束后还原）。"""
    job_budget(600)
    store = _Store(None)
    errors = _JobErrors()
    seen: list[float] = []

    async def execute(job, **kwargs):
        seen.append(dl.get_remaining_budget())
        assert dl.is_job_scope() is True, "执行循环内必须是任务作用域"

    loop = _loop(store, errors, execute)
    await loop.run("job-1")
    assert seen, "执行体没被调用"
    assert seen[0] > 0
    assert dl.is_job_scope() is False, "run 结束后必须还原（不能把任务预算泄漏给调用方）"


@pytest.mark.asyncio
async def test_execution_loop_maps_budget_exhaustion_to_stable_code():
    """预算耗尽 → ``JOB_DEADLINE_EXCEEDED`` + 可行动文案（不是泛化的"超时"）。"""
    store = _Store(None)
    errors = _JobErrors()

    async def execute(job, **kwargs):
        raise dl.DeadlineExceeded("任务预算不足", remaining=0.0, source="job.execution_loop")

    loop = _loop(store, errors, execute)
    await loop.run("job-1")
    assert errors.calls, "失败没有被收敛"
    job_id, message, code = errors.calls[0]
    assert job_id == "job-1"
    assert code == "JOB_DEADLINE_EXCEEDED"
    assert "JOB_DEADLINE_SECONDS" in message, "文案要说清怎么调整（可行动）"


@pytest.mark.asyncio
async def test_temporal_activity_budget_comes_from_node_timeout():
    """Temporal Activity 的预算与 ``start_to_close_timeout`` 同源（节点超时）。"""
    from app.agents.orchestration.temporal import activities

    token = activities._install_node_deadline(42)
    try:
        assert 0 < dl.get_remaining_budget() <= 42
        assert dl.budget_snapshot()["source"] == "job.temporal_activity"
    finally:
        token.reset()
    # 关闭（0/负）时不限制，但仍然不继承任何请求预算
    dl.set_deadline(3.0, source="orchestrator.stream")
    assert activities._install_node_deadline(0) is None
    assert dl.get_remaining_budget() == float("inf")


@pytest.mark.asyncio
async def test_job_budget_is_per_task_not_shared(job_budget):
    """并发任务各拿一份预算（contextvars 在每个 asyncio 任务里是副本）。"""
    job_budget(600)
    seen: list[tuple[str, bool]] = []

    async def worker(name: str, budget: float):
        token = dl.install_job_deadline(budget=budget, source=f"job.{name}")
        try:
            await asyncio.sleep(0)
            seen.append((name, dl.get_remaining_budget() <= budget))
        finally:
            token.reset()

    await asyncio.gather(worker("a", 5.0), worker("b", 500.0))
    assert dict(seen) == {"a": True, "b": True}
