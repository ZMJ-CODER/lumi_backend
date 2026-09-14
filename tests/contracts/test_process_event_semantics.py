"""执行过程帧的语义字段回归（后端判定 kind + 非空 title/summary + 稳定去重键）。

背景：``SseEventEncoder`` 只能复制它拿到的字段。如果发射点（执行内核
``lumi_execution.step_engine`` / 协议层 / ``app.services.orchestrator``）不给
``entry_id``/``kind``/``title``/``summary``，实时帧就是**空行**，用户只能靠刷新
等 ``app/contracts/process_log.py`` 从 Job 快照补出来。

本文件锁定四件事：

1. 一次 ``step_started`` → ``tool_started`` → ``process`` → ``tool_completed`` →
   ``step_completed`` 序列里，每帧的统一过程字段都非空，且 ``kind`` 是合法
   ``ProcessKind``、``status`` 是合法 ``ProcessStatus``；
2. id 稳定：``tool_started``/``tool_completed`` 共用同一 ``call:<call_id>``，
   ``step_started``/``step_completed`` 共用 ``step:<step_id>``（与持久化去重键
   一致，刷新后同一行）；
3. 安全：**过程字段**里绝不出现原始工具参数、绝对本地路径、凭据、DSML/XML；
   任何帧里都不出现原始 ``arguments``/凭据/协议原文；
4. 加法：原有字段（type/job_id/step_id/call_id/content/status/tool/
   result_summary/run_view）一个不少。

范围说明：``run_view.steps[].description`` 是既有的**计划文本**通道（本次不改、
不在过程日志契约内），它照原样携带计划说明，因此"绝对路径/文件名"的断言只针对
过程字段；凭据与原始参数在任何帧里都不允许出现（不走计划文本通道）。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from lumi_contracts import ProcessKind, ProcessStatus

from app.agents.orchestration.models import Job, JobStatus, TaskNode, TaskStatus
from app.agents.orchestration.execution.review import NoopReviewer
from app.agents.orchestration.runtime.state import InMemoryStateStore
from app.agents.orchestration.step_run_service import StepRunService
from app.contracts.events import SseEventEncoder
from app.repositories.job_repository import StateStoreJobRepository

from lumi_execution.step_contract import StepOutcome, StepRunState
from lumi_execution.step_engine import StepRunEngine

# ── 夹具与断言工具 ──────────────────────────────────────────────────

_VALID_KINDS = {str(item) for item in ProcessKind}
_VALID_STATUS = {str(item) for item in ProcessStatus}


@pytest.fixture(autouse=True)
def _available_effect_journal(monkeypatch):
    """让"写工具"步骤在测试里能真正跑完。

    写类工具有副作用，节点执行会先问副作用安全日志。其它测试
    （``tests/test_effect_journal.py``）会把日志替换成不可用实现来验证阻断路径，
    那是**模块级全局**，会污染后续用例；这里显式装回内存实现，保证本文件的用例
    与执行顺序无关（monkeypatch 结束时会还原原值）。
    """
    from app.agents.orchestration.runtime import effects
    from app.repositories.effect_journal_repository import InMemoryEffectJournalRepository

    monkeypatch.setattr(effects, "_repository", InMemoryEffectJournalRepository())

_LEAKY_PATH = "C:\\Users\\me\\secret.py"
_LEAKY_TOKEN = "sk-live-abcdef123456"

# 任何帧都不允许出现的原始载荷痕迹（凭据/参数名/协议原文）。
_FORBIDDEN_ANYWHERE = ("arguments", "sk-live", "abcdef123456", "<dsml", "dsml", "<||")
# 过程字段里额外不允许出现的本地路径痕迹。
_FORBIDDEN_IN_PROCESS = ("C:\\Users", "/Users/", "secret.py", _LEAKY_PATH)

_PROCESS_KEYS = (
    "entry_id", "kind", "title", "summary", "detail", "safe_detail", "tool_name", "status",
)
# 本文件断言"无原始载荷"的帧类型（过程/步骤通道；task_failed.error 等既有错误
# 通道不在本次改动范围）。
_PROCESS_FRAME_TYPES = frozenset({
    "process", "step", "step_started", "step_completed", "plan_ready",
    "tool_started", "tool_completed", "waiting_approval",
})
# 内核一次成功单步必然产出的帧序列（process 帧由宿主过程文本触发，单独覆盖）。
_STEP_SEQUENCE = ("step_started", "tool_started", "tool_completed", "step_completed")


def _frame(line: str) -> dict:
    return json.loads(line[len("data: "):].strip())


def _process_text(frame: dict) -> str:
    return json.dumps({key: frame.get(key) for key in _PROCESS_KEYS}, ensure_ascii=False)


def _assert_semantic_frame(frame: dict, *, event_type: str) -> None:
    """统一过程字段必须非空、kind/status 合法、无原始载荷痕迹。"""
    assert frame["type"] == event_type
    for key in ("entry_id", "kind", "title", "summary"):
        assert str(frame.get(key) or "").strip(), f"{event_type} 帧缺少 {key}: {frame}"
    assert frame["kind"] in _VALID_KINDS, frame
    assert frame["status"] in _VALID_STATUS, frame
    blob = _process_text(frame)
    for forbidden in _FORBIDDEN_IN_PROCESS:
        assert forbidden not in blob, f"{event_type} 过程字段泄露 {forbidden}: {blob}"


def _assert_no_raw_payload(frames: list[dict], *, process_only: bool = True) -> None:
    """过程通道的帧不得携带原始参数/凭据/协议原文。"""
    if process_only:
        frames = [frame for frame in frames if frame["type"] in _PROCESS_FRAME_TYPES]
    blob = json.dumps(frames, ensure_ascii=False)
    for forbidden in _FORBIDDEN_ANYWHERE:
        assert forbidden not in blob, f"SSE 帧泄露 {forbidden}"


# ── (1) 内核事件序列（StepRunEngine → SSE 编码器）───────────────────


class _FakePorts:
    """最小 StepRunPorts：单步执行时把过程文本推给 on_process。"""

    def __init__(self, state: StepRunState, outcome: StepOutcome, texts=()) -> None:
        self.state = state
        self.outcome = outcome
        self.texts = list(texts)
        self.saved = 0
        self.finalized: list[str] = []

    async def load_state(self, job_id: str) -> StepRunState | None:
        return self.state

    async def save_state(self, state: StepRunState) -> None:
        self.state = state
        self.saved += 1

    async def acquire_capacity(self, state: StepRunState) -> bool:
        return True

    async def release_capacity(self, state: StepRunState) -> None:
        return None

    async def execute_step(self, state, step_id, on_process) -> StepOutcome:
        for text in self.texts:
            await on_process(text)
        return self.outcome

    async def handle_escalation(self, state: StepRunState) -> None:
        return None

    async def finalize_completed(self, state: StepRunState) -> None:
        self.finalized.append("completed")

    async def finalize_failed(self, state: StepRunState) -> None:
        self.finalized.append("failed")


def _kernel_state(*, tool: str = "workspace_stage_write") -> StepRunState:
    return StepRunState(
        job_id="job-sem-1",
        user_id="u1",
        job_status="pending",
        canonical="waiting_run",
        plan_revision=1,
        current_step_index=0,
        steps=[
            {
                "id": "s1",
                "title": "写入结果",
                # 说明里带绝对路径：内核只允许把**净化后**的摘要放进过程字段。
                "description": f"写入 {_LEAKY_PATH} 并保存",
                "domain": "workspace",
                "status": "pending",
                "tool": tool,
                # 原始参数永不出现在任何过程字段里。
                "arguments": {"path": _LEAKY_PATH, "token": _LEAKY_TOKEN},
            }
        ],
    )


def _collect_kernel_events(ports: _FakePorts) -> list[dict]:
    engine = StepRunEngine(
        ports=ports,
        view_builder=lambda state: {
            "status": state.canonical,
            "steps": [],
            "final_answer": state.final_answer,
        },
        poll_interval=0.01,
    )

    async def scenario():
        return [
            event
            async for event in engine.run_next_stream(job_id="job-sem-1", idempotency_key="k1")
        ]

    return asyncio.run(scenario())


def _encode(events: list[dict], *, job_id: str = "") -> list[dict]:
    encoder = SseEventEncoder(job_id=job_id)
    return [_frame(encoder.encode(event)) for event in events]


def test_kernel_step_process_and_tool_frames_carry_semantic_fields():
    ports = _FakePorts(
        _kernel_state(),
        StepOutcome(
            status="completed",
            result_summary="已保存到工作区",
            result_ref={"ref": "r1"},
        ),
        texts=["我先整理一下要写入的内容。"],
    )
    frames = _encode(_collect_kernel_events(ports), job_id="job-sem-1")
    by_type: dict[str, dict] = {frame["type"]: frame for frame in frames}

    # ── 原字段一个不少（前端与旧测试依赖）──
    started = by_type["step_started"]
    assert started["job_id"] == "job-sem-1"
    assert started["step_id"] == "s1" and started["step_index"] == 0
    assert started["plan_revision"] == 1

    # ── 语义字段非空、kind 由工具名判定（workspace_stage_write → edit）──
    _assert_semantic_frame(started, event_type="step_started")
    assert started["kind"] == "edit"
    assert started["status"] == "running"
    assert started["entry_id"] == "step:s1"
    assert started["title"] == "写入结果"
    assert "[路径]" in started["summary"]

    tool_started = by_type["tool_started"]
    _assert_semantic_frame(tool_started, event_type="tool_started")
    assert tool_started["tool"] == "workspace_stage_write"
    assert tool_started["call_id"]
    assert tool_started["entry_id"] == f"call:{tool_started['call_id']}"
    assert tool_started["status"] == "running"

    process = by_type["process"]
    _assert_semantic_frame(process, event_type="process")
    assert process["step_id"] == "s1"
    assert process["entry_id"] == "process:s1:1"
    assert process["kind"] == "thinking"
    # content 原样保留（向后兼容），但摘要只能是公开进度短语，不复制正文。
    assert process["content"] == "我先整理一下要写入的内容。"
    assert process["summary"] != process["content"]
    assert "整理一下" not in process["summary"]

    tool_completed = by_type["tool_completed"]
    _assert_semantic_frame(tool_completed, event_type="tool_completed")
    assert tool_completed["tool"] == "workspace_stage_write"
    assert tool_completed["status"] == "completed"
    # started/completed 共用同一 call_id 与 entry_id → 刷新/重连后同一行。
    assert tool_completed["call_id"] == tool_started["call_id"]
    assert tool_completed["entry_id"] == tool_started["entry_id"]

    step_done = by_type["step_completed"]
    _assert_semantic_frame(step_done, event_type="step_completed")
    assert step_done["status"] == "completed"
    assert step_done["entry_id"] == "step:s1"
    assert step_done["kind"] == "edit"
    # 步骤级 result_summary 字段保留原名原义（明确不是最终答复）。
    assert step_done["result_summary"] == "已保存到工作区"
    # 终态 run_view 通道不受语义字段影响。
    assert by_type["done"]["run_view"] == {
        "status": "completed",
        "steps": [],
        "final_answer": "",
    }

    # 过程字段不含路径/参数；全帧不含凭据/原始参数/协议原文。
    _assert_no_raw_payload(frames)


def test_kernel_tool_completed_status_and_waiting_approval_fields():
    ports = _FakePorts(
        _kernel_state(tool="run_in_sandbox"),
        StepOutcome(
            status="waiting_approval",
            result_summary=f"需要确认后执行（{_LEAKY_PATH}，token={_LEAKY_TOKEN}）",
        ),
    )
    frames = _encode(_collect_kernel_events(ports), job_id="job-sem-1")
    by_type: dict[str, dict] = {frame["type"]: frame for frame in frames}

    # 工具名 run_in_sandbox → command；等待审批时过程状态是 running（不是 completed）。
    assert by_type["tool_started"]["kind"] == "command"
    tool_completed = by_type["tool_completed"]
    _assert_semantic_frame(tool_completed, event_type="tool_completed")
    assert tool_completed["status"] == "running"
    assert tool_completed["entry_id"] == by_type["tool_started"]["entry_id"]

    # waiting_approval 不在 SSE 出口的统一字段集合里：字段原样上线，因此 status
    # 保持既有审批语义（前端按它显示审批态），其余过程字段同样必须非空且安全。
    approval = by_type["waiting_approval"]
    assert approval["status"] == "waiting_approval"
    assert approval["step_id"] == "s1"
    assert approval["entry_id"] == by_type["tool_started"]["entry_id"]
    assert approval["kind"] == "command"
    assert str(approval["title"]).strip() and str(approval["summary"]).strip()
    assert approval["risk"] and approval["run_view"]
    assert "[路径]" in approval["summary"] or "[已隐藏]" in approval["summary"]
    _assert_no_raw_payload(frames)


def test_kernel_failed_step_completion_is_sanitized():
    ports = _FakePorts(
        _kernel_state(),
        StepOutcome(
            status="failed",
            error=f"写入失败：{_LEAKY_PATH} 被拒绝（token={_LEAKY_TOKEN}）",
            error_code="STEP_FAILED",
        ),
    )
    frames = _encode(_collect_kernel_events(ports), job_id="job-sem-1")
    by_type: dict[str, dict] = {frame["type"]: frame for frame in frames}

    step_done = by_type["step_completed"]
    _assert_semantic_frame(step_done, event_type="step_completed")
    assert step_done["status"] == "failed"
    assert step_done["entry_id"] == "step:s1"
    # 步骤级字段保留原名，但内容已净化。
    assert "result_summary" in step_done
    assert "C:\\Users" not in step_done["result_summary"]
    assert "sk-live" not in step_done["result_summary"]
    _assert_no_raw_payload(frames)


def test_process_frame_summary_never_copies_model_prose():
    """``process`` 帧的摘要只能是公开进度短语，不复制正文/推理。"""
    ports = _FakePorts(
        _kernel_state(),
        StepOutcome(status="completed", result_summary="done"),
        texts=["模型内部完整推理：先看 A 再看 B，然后调用工具。"],
    )
    frames = _encode(_collect_kernel_events(ports), job_id="job-sem-1")
    process = next(frame for frame in frames if frame["type"] == "process")
    assert process["summary"] == "正在处理：写入 [路径] 并保存"
    assert "推理" not in process["summary"]


# ── (2) app 层 run_next 全链路（StepRunService → SSE 编码器）────────


class _FakeWorker:
    def __init__(self, *, output: str = "已完成写入", fail: bool = False) -> None:
        self.calls = 0
        self.output = output
        self.fail = fail

    async def execute(self, node: TaskNode, ctx) -> dict:
        self.calls += 1
        if self.fail:
            raise RuntimeError("worker boom")
        return {"success": True, "content": self.output, "tool": "workspace_stage_write"}


def _service(*, repo, worker) -> StepRunService:
    async def noop_capacity(_job) -> bool:
        return True

    return StepRunService(
        store=repo,
        workers={"w1": worker},
        review=NoopReviewer(),
        finalizer=object(),
        llm_configs={},
        ensure_active_capacity=noop_capacity,
        poll_interval=0.02,
    )


async def _make_run_next_job() -> tuple[StateStoreJobRepository, Job]:
    node = TaskNode(
        id="s1",
        name="写入结果",
        agent="w1",
        # 原始工具参数只出现在节点参数里（永不进入过程展示字段）。
        params={
            "preferred_tool": "workspace_stage_write",
            "instruction": f"写入 {_LEAKY_PATH} 并保存",
            "arguments": {"path": _LEAKY_PATH, "token": _LEAKY_TOKEN},
        },
    )
    repo = StateStoreJobRepository(InMemoryStateStore())
    job = Job(
        job_id="job-run-next-sem",
        user_id="u1",
        user_role="user",
        request="逐步骤执行语义字段测试",
        scene="office",
        status=JobStatus.PENDING,
        nodes=[node],
        routing={
            "execution_mode": "step_confirm",
            "execution_state": "waiting_run",
            "plan_revision": 1,
            "current_step_index": 0,
            "steps": [
                {
                    "id": "s1",
                    "title": "写入结果",
                    "description": f"写入 {_LEAKY_PATH} 并保存",
                    "domain": "w1",
                    "status": "pending",
                    "result_ref": None,
                    "tool": "workspace_stage_write",
                }
            ],
        },
    )
    await repo.create_job(job)
    return repo, job


async def _run_next(*, key: str, fail: bool = False):
    """返回 (raw_events, frames, job 快照, 步骤节点)。"""
    repo, job = await _make_run_next_job()
    service = _service(repo=repo, worker=_FakeWorker(fail=fail))
    raw = [
        event
        async for event in service.run_next_stream(
            job_id=job.job_id, idempotency_key=key, workspace_bound=True,
        )
    ]
    frames = _encode(raw, job_id=job.job_id)
    snapshot = await repo.get_job(job.job_id)
    return raw, frames, snapshot, snapshot.nodes[0]


def test_run_next_stream_frames_are_semantically_complete_and_safe():
    raw, frames, snapshot, node = asyncio.run(_run_next(key="k1"))
    by_type: dict[str, list[dict]] = {}
    for frame in frames:
        by_type.setdefault(frame["type"], []).append(frame)

    # ── 原有字段一个不少：plan_ready/step{...}/run_view 通道不受影响 ──
    assert by_type["step_started"][0]["step_id"] == "s1"
    done = by_type["done"][0]
    assert done["run_view"]["steps"][0]["id"] == "s1"
    assert done["run_view"]["status"] == "completed"
    assert snapshot.routing["steps"][0]["status"] == "completed"
    assert snapshot.nodes[0].status == TaskStatus.COMPLETED

    # ── 序列里的每一类过程帧都带非空语义字段 ──
    for event_type in _STEP_SEQUENCE:
        assert by_type.get(event_type), f"缺少 {event_type} 帧"
        for frame in by_type[event_type]:
            _assert_semantic_frame(frame, event_type=event_type)
    # ── id 稳定：与持久化去重键一致（step:<id> / call:<call_id>）──
    assert by_type["step_started"][0]["entry_id"] == "step:s1"
    assert by_type["step_completed"][0]["entry_id"] == "step:s1"
    assert by_type["tool_started"][0]["entry_id"] == by_type["tool_completed"][0]["entry_id"]
    assert by_type["tool_started"][0]["call_id"] == by_type["tool_completed"][0]["call_id"]
    assert by_type["tool_started"][0]["entry_id"].startswith("call:")

    # ── 安全：过程字段不含路径；任何帧不含原始参数/凭据/协议原文 ──
    # app 层现在注入面向用户文案（与刷新投影同源），因此摘要来自 presentation；
    # 内核的声明文案（含被净化的 instruction）不再进入实时过程字段。
    from app.agents.orchestration.execution.presentation import working_text

    assert by_type["step_started"][0]["summary"] == working_text(node)
    assert "C:\\Users" not in _process_text(by_type["step_started"][0])
    _assert_no_raw_payload(frames)
    # 工具级状态词汇保留在 raw 事件里（旧字段语义不丢），过程状态映射为契约状态。
    tool_completed_raw = next(e for e in raw if e["type"] == "tool_completed")
    assert tool_completed_raw["tool_status"] == "success"
    assert tool_completed_raw["status"] == "completed"


def test_run_next_failed_step_keeps_error_surface_and_semantics():
    raw, frames, _snapshot, _node = asyncio.run(_run_next(key="k2", fail=True))
    by_type = {frame["type"]: frame for frame in frames}
    step_done = by_type["step_completed"]
    _assert_semantic_frame(step_done, event_type="step_completed")
    assert step_done["status"] == "failed"
    assert step_done["entry_id"] == "step:s1"
    assert "result_summary" in step_done
    # 失败终态通道保持原样（error/error_code/run_view 字段一个不少）。
    task_failed = by_type["task_failed"]
    assert task_failed["status"] == "failed"
    assert str(task_failed["error_code"]).strip()
    assert "error" in task_failed and task_failed["retryable"] is False
    assert task_failed["run_view"]["status"] == "failed"
    _assert_no_raw_payload(frames)


# ── (3) 协议层：process/tool 事件也带语义字段 ───────────────────────


def test_protocol_chunk_events_carry_semantic_fields():
    from lumi_orch.protocol import NormalizedToolCall, ParsedModelChunk, chunk_to_events

    chunk = ParsedModelChunk(
        process_delta="我先查看一下工作区资料。",
        tool_calls=[
            NormalizedToolCall(
                name="workspace_read",
                arguments={"path": _LEAKY_PATH, "token": _LEAKY_TOKEN},
                call_id="call-42",
                protocol="dsml_attribute",
            )
        ],
    )
    events = {event["type"]: event for event in chunk_to_events(chunk)}

    process = events["process"]
    assert process["kind"] == "thinking"
    assert process["title"] and process["summary"]
    assert process["status"] == "running"
    assert process["content"] == "我先查看一下工作区资料。"

    tool = events["tool"]
    assert tool["kind"] == "read"
    assert tool["entry_id"] == "call:call-42"
    assert tool["call_id"] == "call-42"
    assert tool["status"] == "running"
    assert tool["title"] and tool["summary"]
    # 原始 arguments 只留在既有 tool_call 字段里，绝不复制进过程展示字段。
    assert tool["tool_call"]["arguments"] == {"path": _LEAKY_PATH, "token": _LEAKY_TOKEN}
    for key in ("title", "summary", "tool_name", "entry_id", "kind", "status"):
        assert _LEAKY_PATH not in str(tool[key])
        assert _LEAKY_TOKEN not in str(tool[key])

    # 编码后的统一字段同样非空且安全（原始 tool 事件不在 _PROCESS_EVENT_TYPES 里，
    # 语义字段由协议层直接给出）。
    frames = _encode(list(events.values()), job_id="job-proto")
    _assert_no_raw_payload(frames)

def test_orchestrator_step_and_plan_frames_carry_semantic_fields():
    from app.services.orchestrator import _process_fields, _step_frame_fields

    step_fields = _step_frame_fields({
        "id": "s1",
        "title": "写入结果",
        "status": "completed",
        "tool": "workspace_stage_write",
        "output": f"已写入 {_LEAKY_PATH}",
        "error": None,
    })
    assert step_fields["entry_id"] == "step:s1"
    assert step_fields["kind"] == "edit"
    assert step_fields["status"] == "completed"
    assert step_fields["title"] and step_fields["summary"]
    assert "C:\\Users" not in step_fields["summary"]

    pending_fields = _step_frame_fields({"id": "s2", "title": "生成报告", "status": "pending"})
    assert pending_fields["entry_id"] == "step:s2"
    assert pending_fields["kind"] == "thinking"
    assert pending_fields["status"] == "pending"
    assert pending_fields["summary"]

    plan_fields = _process_fields(
        entry_id="plan:r2",
        kind="thinking",
        title="执行计划",
        summary="1. 读取\n2. 写入",
        status="completed",
    )
    assert plan_fields == {
        "entry_id": "plan:r2",
        "kind": "thinking",
        "title": "执行计划",
        "summary": "1. 读取 2. 写入",
        "status": "completed",
    }


def test_orchestrator_step_frame_wire_fields_are_non_empty_and_dedup_stable():
    """office 自动路径的 ``{"type": "step", ...}`` 帧必须能直接渲染。

    修复前：该帧只有 ``step.output``，统一字段里 ``summary`` 为空、``entry_id``
    退化成流内序号（``seq:job:3``），与刷新的 ``step:<step_id>`` 不能合并。
    """
    from app.services.orchestrator import _step_frame_fields

    step = {
        "id": "s1",
        "title": "步骤 s1",
        "status": "completed",
        "runtime_status": "completed",
        "tool": "office_doc_read",
        "output": "读取完成，共 3 页",
    }
    frames = _encode([{"type": "step", "job_id": "job-1", "step": step, **_step_frame_fields(step)}],
                     job_id="job-1")
    frame = frames[0]
    assert frame["step"] == step  # 原字段原样保留
    assert frame["entry_id"] == "step:s1"
    assert frame["kind"] == "read"
    assert frame["status"] == "completed"
    assert frame["title"] == "步骤 s1"
    assert frame["summary"] == "读取完成，共 3 页"
    assert frame["tool_name"] == "office_doc_read"
    assert frame["sequence"] == 1 and frame["occurred_at"]
    _assert_no_raw_payload(frames)


def test_step_frame_fields_status_and_unknown_kind_are_normalized():
    """非法 kind/status 由契约收敛（不向前端发明新枚举），空字段也不留空行。"""
    from app.services.orchestrator import _process_fields

    fields = _process_fields(
        kind="not-a-kind", title="步骤", summary="正在执行", status="running",
    )
    assert fields["kind"] in _VALID_KINDS
    assert fields["kind"] == "thinking"
    assert fields["status"] in _VALID_STATUS

    unknown_status = _process_fields(
        kind="thinking", title="步骤", summary="正在执行", status="retrying-not-a-status",
    )
    assert unknown_status["status"] == "running"

    # 兜底：空 title/summary 也要给出可渲染的占位，不留空行。
    blank = _process_fields(kind="thinking", title="", summary="", status="running")
    assert blank["title"] and blank["summary"]
