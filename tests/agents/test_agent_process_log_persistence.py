"""执行过程日志的持久化与刷新恢复回归（"后端缺口"闭环）。

契约（``ProcessLogEntry``/``merge_process_log``）与 SSE 出口已经就绪，但过程只活在
流里：页面刷新后 ``GET /agents/jobs/{job_id}`` 没有任何可恢复的过程。这里覆盖补齐
缺口后的行为：

  (a) 完成/单步 Job 快照 → ``run_view.process_log`` 非空、有序、去重；
  (b) 重复保存/重复读取不产生重复条目；
  (c) 条目永不携带原始参数/绝对路径/凭据；
  (d) 刷新恢复：mapper + 端点 helper 连调两次结果一致（含 JSON 往返）；
  (e) 步骤 ``result_summary`` 不得被当成最终答复（final_answer 只来自
      ``job.result.final_answer``）。
"""

from __future__ import annotations

import asyncio
import json

from lumi_contracts import ProcessLogEntry, ProcessStatus

from deadline import with_deadline

from app.agents.orchestration.models import Job, JobStatus, TaskNode
from app.agents.orchestration.execution.review import NoopReviewer
from app.agents.orchestration.runtime.state import InMemoryStateStore
from app.agents.orchestration.step_run_service import StepRunService
from app.api.v1 import agents as agents_api
from app.contracts.process_log import (
    merge_job_process_log,
    persist_process_log,
    process_log_from_job,
    process_log_payload,
)
from app.repositories.job_repository import StateStoreJobRepository


# ── 复用 tests/test_step_run_service.py 的编排夹具（InMemoryStateStore + 假 worker）──


class _FakeWorker:
    def __init__(self, *, fail: bool = False, output: str = "ok") -> None:
        self.calls = 0
        self.fail = fail
        self.output = output

    async def execute(self, node: TaskNode, ctx) -> dict:
        self.calls += 1
        if self.fail:
            raise RuntimeError("worker boom")
        return {"success": True, "content": self.output, "tool": "fake"}


def _node(nid: str, *, dep: str | None = None, tool: str = "", name: str = "") -> TaskNode:
    params: dict = {"instruction": f"do {nid}"}
    if tool:
        params["preferred_tool"] = tool
    return TaskNode(
        id=nid,
        name=name or f"步骤 {nid}",
        agent="w1",
        params=params,
        depends_on=[dep] if dep else [],
    )


def _make_repo(
    *,
    nodes: list[TaskNode],
    state: str = "waiting_run",
    plan_text: str = "",
) -> tuple[StateStoreJobRepository, Job]:
    store = InMemoryStateStore()
    repo = StateStoreJobRepository(store)
    steps = []
    for node in nodes:
        tool = str((node.params or {}).get("preferred_tool") or "")
        step = {
            "id": node.id,
            "title": node.name or node.id,
            "description": "",
            "domain": node.agent,
            "status": "pending",
            "result_ref": None,
        }
        if tool:
            step["tool"] = tool
        steps.append(step)
    job = Job(
        job_id="plan-job-1",
        user_id="u1",
        user_role="user",
        request="逐步骤执行测试",
        scene="office",
        status=JobStatus.PENDING,
        nodes=nodes,
        routing={
            "execution_mode": "step_confirm",
            "execution_state": state,
            "plan_revision": 1,
            "current_step_index": 0,
            "plan_text": plan_text,
            "steps": steps,
        },
    )
    return repo, job


def _service(*, repo, worker) -> StepRunService:
    async def noop_capacity(_job) -> bool:
        return True

    async def noop_finalize(_job) -> None:
        return None

    service = StepRunService(
        store=repo,
        workers={"w1": worker},
        review=NoopReviewer(),
        finalizer=object(),
        llm_configs={},
        ensure_active_capacity=noop_capacity,
        finalize_completed=noop_finalize,
        finalize_failed=noop_finalize,
        poll_interval=0.02,
    )
    return service


async def _collect(service: StepRunService, *, key: str) -> list[dict]:
    return [
        event
        async for event in service.run_next_stream(
            job_id="plan-job-1",
            idempotency_key=key,
            workspace_bound=True,
        )
    ]


def _job_with_steps(
    *,
    steps: list[dict],
    nodes: list[TaskNode] | None = None,
    plan_text: str = "",
    state: str = "completed",
    final_answer: str = "",
) -> Job:
    return Job(
        job_id="job-refresh-1",
        user_id="u1",
        user_role="user",
        request="刷新恢复测试",
        scene="office",
        status=JobStatus.COMPLETED,
        result={"final_answer": final_answer} if final_answer else None,
        nodes=nodes or [],
        routing={
            "execution_mode": "step_confirm",
            "execution_state": state,
            "plan_revision": 1,
            "current_step_index": len(steps),
            "plan_text": plan_text,
            "steps": steps,
        },
    )


class _OrchestratorStub:
    """只暴露端点需要的 ``get_job``，让测试不触碰真实编排单例。"""

    def __init__(self, job: Job | None) -> None:
        self._job = job

    async def get_job(self, job_id: str) -> Job | None:
        if self._job is not None and self._job.job_id == job_id:
            return self._job
        return None


def _endpoint_data(monkeypatch, job: Job) -> dict:
    monkeypatch.setattr(agents_api, "orchestrator", _OrchestratorStub(job))
    return asyncio.run(agents_api.get_agent_job(job.job_id, {"sub": job.user_id}))["data"]


def _endpoint_run_view(monkeypatch, job: Job) -> dict:
    return _endpoint_data(monkeypatch, job)["run_view"]


# ── (a) 完成/单步 Job → 非空、有序、去重的 run_view.process_log ──────────


def test_completed_step_job_exposes_ordered_deduped_process_log(monkeypatch):
    async def scenario() -> Job:
        repo, job = _make_repo(
            nodes=[
                _node("s1", tool="office_doc_read"),
                _node("s2", dep="s1", tool="workspace_stage_write"),
            ],
            plan_text="1. 阅读文档\n2. 写入结果",
        )
        await repo.create_job(job)
        service = _service(repo=repo, worker=_FakeWorker(output="step-out"))
        await _collect(service, key="k1")
        await _collect(service, key="k2")
        return await repo.get_job(job.job_id)

    saved = asyncio.run(scenario())

    # 刷新：重新反序列化后走端点（真实刷新路径）
    refreshed = Job.model_validate_json(saved.model_dump_json())
    view = _endpoint_run_view(monkeypatch, refreshed)
    log = view["process_log"]

    assert log, "刷新后必须有可恢复的过程日志"
    assert [row["entry_id"] for row in log] == ["plan:r1", "step:s1", "step:s2"]
    assert [row["sequence"] for row in log] == [0, 1, 2]
    assert [row["status"] for row in log] == ["completed", "completed", "completed"]
    # kind 由后端判定：计划=thinking，阅读=read，写盘=edit
    assert [row["kind"] for row in log] == ["thinking", "read", "edit"]
    assert {row["job_id"] for row in log} == {refreshed.job_id}
    # 过程日志落 Job 快照，不落 routing（routing 只存路由/策略）
    assert refreshed.process_log
    assert "process_log" not in refreshed.routing
    # JSON-safe：端点载荷可直接序列化
    assert json.loads(json.dumps(log, ensure_ascii=False)) == log


def test_auto_run_job_exposes_process_log_after_refresh(monkeypatch):
    """自动执行（非单步）路径同样可恢复：读取时由 Job 现状现推导。

    本用例走真实 ``submit_job``，因此会经过计划编译期的**桌面 MCP 能力发现**。
    离线环境（CI / 没有客户端）里那是"连接超时 5s 再降级"，而规划被编译器拒绝时
    会再走一次带反馈重规划 → **两次 5s 超时**，恰好压在 M1（10s）档位上，于是这个
    用例会随机器负载时通时断（实测：单跑 2.1s，全量套件里超 10s）。

    这里断言的是**过程日志的持久化与刷新恢复**，与"桌面客户端能不能连上"无关，
    所以直接把 MCP 能力发现置空：既保留真实 ``submit_job`` 路径，又不让用例的成败
    取决于本机有没有开客户端。
    """
    from app.agents.orchestration.orchestrator import AgentOrchestrator
    from app.agents.orchestration.planning.planner import Planner, TaskTree
    from app.agents.skills import executor as skills_executor
    from app.core.config import settings

    async def _no_mcp_capabilities(*_args, **_kwargs):
        return []

    monkeypatch.setattr(skills_executor, "get_desktop_mcp_capabilities", _no_mcp_capabilities)

    class _RecordingWorker:
        async def execute(self, node, ctx):
            return {"success": True, "content": f"result-{node.id}", "tool": "fake"}

    class _FakePlanner(Planner):
        async def plan(self, *args, **kwargs):
            return TaskTree(nodes=[_node("s1")], plan_text="计划文本")

        async def plan_for_level(self, *_args, **_kwargs):
            return TaskTree(nodes=[_node("s1")], plan_text="计划文本")

    monkeypatch.setattr(settings, "TASK_ROUTER_V2_ENABLED", False)
    monkeypatch.setattr(settings, "EXECUTION_POLICY_V2_ENABLED", False)
    orch = AgentOrchestrator(
        store=InMemoryStateStore(),
        planner=_FakePlanner(),
        workers={"w1": _RecordingWorker()},
        review=NoopReviewer(),
        temporal_enabled=False,
    )

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_record_office_summary", noop)
    monkeypatch.setattr(orch, "_record_office_task_index", noop)

    async def scenario() -> Job:
        job = await orch.submit_job("u1", "读一下 README 并总结", conversation_id="c1")
        await asyncio.gather(*orch._tasks.values())
        return await orch.get_job(job.job_id)

    final = asyncio.run(
        with_deadline(
            scenario(), label="自动执行路径 submit_job + 过程日志恢复",
            # 走到真实 submit_job：离线环境下要先等 MCP 能力发现超时再降级。
            tier="m1",
        )
    )
    view = _endpoint_run_view(monkeypatch, final)
    log = view["process_log"]
    assert log
    ids = [row["entry_id"] for row in log]
    assert len(ids) == len(set(ids))
    assert any(entry_id.startswith("step:") for entry_id in ids)
    assert "process_log" not in final.routing


# ── (b) 重复保存不重复 ─────────────────────────────────────────────


def test_repeated_saves_and_reads_do_not_duplicate_entries():
    async def scenario():
        repo, job = _make_repo(nodes=[_node("s1"), _node("s2", dep="s1")])
        await repo.create_job(job)
        service = _service(repo=repo, worker=_FakeWorker())
        await _collect(service, key="k1")
        first = await repo.get_job(job.job_id)
        first_ids = [row["entry_id"] for row in first.process_log]
        assert len(first_ids) == len(set(first_ids))
        # 同一次快照反复合并 + 再保存一次（重复保存路径）
        assert persist_process_log(first) == len(first_ids)
        assert persist_process_log(first) == len(first_ids)
        await repo.save_job(first)
        again = await repo.get_job(job.job_id)
        assert [row["entry_id"] for row in again.process_log] == first_ids
        # 再跑第二步（状态推进）：条目集合不变，只是状态前进
        await _collect(service, key="k2")
        final = await repo.get_job(job.job_id)
        final_ids = [row["entry_id"] for row in final.process_log]
        assert final_ids == first_ids
        assert len(final_ids) == len(set(final_ids))


# ── (c) 安全摘要：原始参数/绝对路径/凭据一律不落 ────────────────────


def test_entries_never_carry_raw_arguments_paths_or_credentials():
    node = TaskNode(
        id="s1",
        name="写入结果",
        agent="w1",
        params={
            "preferred_tool": "workspace_stage_write",
            "arguments": {"path": "C:\\Users\\me\\secret.py", "token": "sk-live-abcdef123456"},
            "instruction": "写入 C:\\Users\\me\\secret.py",
        },
        result={"content": "已写入 /Users/me/secret.py", "raw": "<dsml>secret</dsml>"},
    )
    job = _job_with_steps(
        nodes=[node],
        steps=[
            {
                "id": "s1",
                "title": "写入结果",
                "status": "completed",
                "tool": "workspace_stage_write",
                "result_summary": "已写入 C:\\Users\\me\\secret.py，token=sk-live-abcdef123456",
            }
        ],
    )
    log = process_log_payload(merge_job_process_log(job))
    blob = json.dumps(log, ensure_ascii=False)
    for forbidden in (
        "C:\\Users",
        "/Users/me",
        "sk-live",
        "abcdef123456",
        "arguments",
        "dsml",
        "secret.py",
    ):
        assert forbidden not in blob, f"过程日志泄露了 {forbidden}"
    # 步骤条目只保留安全摘要：不带原始参数，也不带原始结果/推理
    for row in log:
        assert row["call_id"] == ""
        assert row["detail"] == ""
        assert "arguments" not in row and "result" not in row and "reasoning" not in row


# ── (d) 刷新恢复：mapper + 端点 helper 连调两次结果一致 ──────────────


def test_refresh_recovery_is_stable_across_repeated_reads(monkeypatch):
    job = _job_with_steps(
        nodes=[_node("s1", tool="office_doc_read"), _node("s2", dep="s1")],
        steps=[
            {"id": "s1", "title": "步骤 s1", "status": "completed", "tool": "office_doc_read"},
            {"id": "s2", "title": "步骤 s2", "status": "pending"},
        ],
        plan_text="1. 阅读文档\n2. 汇总",
        final_answer="这是任务的最终答复",
    )
    first = process_log_payload(merge_job_process_log(job))
    second = process_log_payload(merge_job_process_log(job))
    assert [row["entry_id"] for row in first] == [row["entry_id"] for row in second]
    assert len(first) == len(second) == 3

    view1 = _endpoint_run_view(monkeypatch, job)
    view2 = _endpoint_run_view(monkeypatch, job)
    assert len(view1["process_log"]) == len(view2["process_log"]) == 3
    assert view1["process_log"] == view2["process_log"]

    # 真实刷新等于"重新反序列化 + 再读"：条目不叠加
    refreshed = Job.model_validate_json(job.model_dump_json())
    assert len(merge_job_process_log(refreshed)) == 3


# ── (e) 步骤摘要不是最终答复 ──────────────────────────────────────


def test_step_summary_is_never_treated_as_final_answer(monkeypatch):
    job = _job_with_steps(
        nodes=[_node("s1")],
        steps=[
            {
                "id": "s1",
                "title": "步骤 s1",
                "status": "completed",
                "result_summary": "STEP-SUMMARY-NOT-FINAL",
            }
        ],
        plan_text="计划",
        final_answer="这是任务的最终答复",
    )
    data = _endpoint_data(monkeypatch, job)
    view = data["run_view"]
    assert view["final_answer"] == "这是任务的最终答复"
    assert "STEP-SUMMARY-NOT-FINAL" not in view["final_answer"]
    assert data["result"]["final_answer"] == "这是任务的最终答复"
    # 过程条目里没有任何"最终答复"语义字段（前端只能按 summary/status 渲染）
    finalish = {"final_answer", "final", "answer", "result", "task_completed", "content"}
    assert not (finalish & set(ProcessLogEntry.model_fields))
    for row in view["process_log"]:
        assert not (finalish & set(row))
    # 步骤摘要只允许出现在过程条目的 summary 上
    step_rows = [row for row in view["process_log"] if row["entry_id"] == "step:s1"]
    assert step_rows and "STEP-SUMMARY-NOT-FINAL" in step_rows[0]["summary"]


# ── 状态映射 / 有界窗口 / 无 step_id 不产出 ────────────────────────


def test_step_status_maps_to_contract_process_status():
    job = _job_with_steps(
        steps=[
            {"id": "p", "title": "a", "status": "pending"},
            {"id": "r", "title": "b", "status": "running"},
            {"id": "c", "title": "c", "status": "completed"},
            {"id": "f", "title": "d", "status": "failed"},
            {"id": "w", "title": "e", "status": "waiting_approval"},
        ],
        state="running_step",
    )
    by_step = {entry.step_id: entry for entry in process_log_from_job(job)}
    assert by_step["p"].status is ProcessStatus.PENDING
    assert by_step["r"].status is ProcessStatus.RUNNING
    assert by_step["c"].status is ProcessStatus.COMPLETED
    assert by_step["f"].status is ProcessStatus.FAILED
    # 等待审批与 SSE 出口一致记为 running
    assert by_step["w"].status is ProcessStatus.RUNNING


def test_process_log_is_bounded_and_needs_stable_step_id():
    steps = [{"id": f"s{index}", "title": f"步骤 {index}", "status": "pending"} for index in range(260)]
    steps.append({"title": "没有 id 的步骤", "status": "pending"})
    job = _job_with_steps(steps=steps, state="waiting_run")
    merged = merge_job_process_log(job)
    assert len(merged) == 200
    assert all(entry.entry_id for entry in merged)
    # 没有稳定 step_id 的步骤不产出条目（否则去重键不稳定，刷新会重复）
    assert all(entry.step_id != "" for entry in merged)


# ── (a) 实时帧与刷新快照同文案（app 层注入 presentation）────────────


def test_live_step_frames_match_process_log_text_for_same_entry():
    """同一步骤：实时 ``step_started``/``step_completed`` 帧与刷新投影逐字一致。

    内核只按步骤声明的 instruction/title 发文案，刷新投影（``process_log_from_job``）
    用的是 ``presentation`` 的面向用户文案；app 层注入后两者必须相同，
    且 ``entry_id`` 不变（同一行，刷新不新增/不重复）。失败态同属一条规则。
    """

    async def run(*, fail: bool) -> tuple[list[dict], Job]:
        nodes = [_node("s1", tool="office_doc_read")]
        repo, job = _make_repo(nodes=nodes, plan_text="1. 阅读文档")
        await repo.create_job(job)
        service = _service(repo=repo, worker=_FakeWorker(output="文档要点一", fail=fail))
        events = await _collect(service, key="k1")
        # 刷新恢复读的是保存后的 Job 快照；用同一次运行留下的快照做对拍。
        return events, (await repo.get_job(job.job_id))

    for fail in (False, True):
        events, snapshot = asyncio.run(run(fail=fail))
        # live：按 entry_id 合并状态推进，最终帧应等于同一步骤终态的文案。
        live: dict[str, dict] = {}
        for event in events:
            entry_id = str(event.get("entry_id") or "")
            if entry_id.startswith("step:"):
                live[entry_id] = event
        assert live, "实时流里必须有步骤过程帧"

        refreshed = {entry.entry_id: entry for entry in process_log_from_job(snapshot)}
        assert set(live) == {
            entry_id for entry_id in refreshed if entry_id.startswith("step:")
        }
        assert set(live) == {"step:s1"}
        for entry_id, frame in live.items():
            # entry_id 不变就是同一行：刷新后不会多出一条重复行。
            assert entry_id == "step:s1"
            assert frame["title"] == refreshed[entry_id].title
            assert frame["summary"] == refreshed[entry_id].summary


def test_job_list_payload_omits_process_log(monkeypatch):
    """列表是多任务响应：过程日志只走详情接口，避免轮询载荷被过程放大。"""

    class _ListStub:
        def __init__(self, job: Job) -> None:
            self._job = job

        async def list_jobs(self, user_id: str, limit: int) -> list[Job]:
            return [self._job]

    job = _job_with_steps(
        steps=[{"id": "s1", "title": "步骤 s1", "status": "completed"}],
        plan_text="计划",
        final_answer="最终答复",
    )
    assert persist_process_log(job) >= 1
    monkeypatch.setattr(agents_api, "orchestrator", _ListStub(job))
    items = asyncio.run(agents_api.list_agent_jobs(5, {"sub": "u1"}))["data"]["items"]
    assert items and "process_log" not in items[0]
    # 详情接口仍然给出过程日志（run_view.process_log）
    assert _endpoint_run_view(monkeypatch, job)["process_log"]
