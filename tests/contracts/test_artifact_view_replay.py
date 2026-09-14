"""产物 / 声明式视图 / 审批标识 / 按 seq 补拉（联调四项的后端侧回归）。

对应前端联调清单：

1. ``artifact_created`` 的受权限保护下载接口 → ``tests`` 里的签名、归属、过期、防篡改；
2. ``approval_required`` 的 node/step 标识 → 事件必须带 ``node_id``（既有审批接口主键）；
3. ``view_updated`` → 只用白名单声明式视图，数据有界、不开放 iframe/HTML/JS；
4. 断线续传 → 任务事件日志按 ``seq`` 补拉，与实时帧同源（同 ``event_id``）。
"""

from __future__ import annotations

import json
from typing import Any

from app.contracts.events import SseEventEncoder
from app.services import artifacts
from app.services import job_event_log
from app.services import views


class _FakeRedis:
    """最小 Redis 替身：只实现事件日志用到的方法。"""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.expires: dict[str, int] = {}

    async def rpush(self, key: str, *values: str) -> int:
        self.lists.setdefault(key, []).extend(values)
        return len(self.lists[key])

    async def ltrim(self, key: str, start: int, end: int) -> bool:
        rows = self.lists.get(key, [])
        self.lists[key] = rows[start:] if end == -1 else rows[start : end + 1]
        return True

    async def expire(self, key: str, seconds: int) -> bool:
        self.expires[key] = int(seconds)
        return True

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        rows = self.lists.get(key, [])
        return rows[start:] if end == -1 else rows[start : end + 1]


def _install_fake_redis(monkeypatch, redis: _FakeRedis) -> None:
    import app.core.redis as redis_module

    monkeypatch.setattr(redis_module, "get_redis", lambda: redis)


class _Node:
    def __init__(self, node_id: str, *, status: str = "completed", outputs=None, metadata=None) -> None:
        self.id = node_id
        self.status = status
        self.result = {"outputs": outputs or []}
        self.metadata = metadata or {}
        self.params = {"preferred_tool": "workspace_write"}
        self.agent = "atomic_step"
        self.started_at = 1.0
        self.completed_at = 1.5
        self.name = f"步骤 {node_id}"


class _Job:
    def __init__(self, job_id: str, nodes: list[_Node]) -> None:
        self.job_id = job_id
        self.nodes = nodes


# ── 1. 产物：签名标识与受权限保护的下载 ──────────────────


def test_artifact_id_is_signed_opaque_and_tamper_proof():
    artifact_id = artifacts.make_artifact_id("job-1", "report.csv")
    parsed = artifacts.parse_artifact_id(artifact_id)
    assert parsed is not None and parsed["container_id"] == "job-1" and parsed["name"] == "report.csv"
    # 签名不符 / 形状不对一律拒绝（不抛错）
    assert artifacts.parse_artifact_id(artifact_id[:-2] + "zz") is None
    assert artifacts.parse_artifact_id("art_not-base64.deadbeefdeadbeef") is None
    assert artifacts.parse_artifact_id("plain-name") is None


def test_artifact_download_respects_owner_expiry_and_traversal(monkeypatch):
    from app.office import docs as office_docs

    out_dir = office_docs.generic_outputs_dir("owner-user", "job-1")
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "report.csv"
    target.write_text("a,b\n1,2\n", encoding="utf-8")

    ref = artifacts.artifact_from_output("job-1", {"name": "report.csv", "size": target.stat().st_size})
    record = artifacts.artifact_record("owner-user", ref["artifact_id"])
    assert record is not None
    assert record["filename"] == "report.csv" and record["mime_type"] == "text/csv"
    assert record["size_bytes"] == target.stat().st_size
    assert record["expires_at"]
    assert artifacts.artifact_path("owner-user", ref["artifact_id"]) == target
    # 归属不符 / 已过期 / 路径穿越（经签名内容）都不能下载
    assert artifacts.artifact_path("another-user", ref["artifact_id"]) is None
    assert artifacts.artifact_path("owner-user", artifacts.make_artifact_id("job-1", "../report.csv")) == target
    assert artifacts.artifact_path("owner-user", "art_" + "x" * 8 + ".0000000000000000") is None

    stale = artifacts.make_artifact_id(
        "job-1", "report.csv", issued_at=0.0
    )
    assert artifacts.artifact_path("owner-user", stale) is None, "签发时间缺失/超过 TTL 必须拒绝下载"


def test_artifacts_for_job_reads_only_safe_outputs():
    job = _Job("job-1", [
        _Node("s1", outputs=[{"name": "a.csv", "size": 10}, {"name": "../evil.sh", "size": 1}]),
        _Node("s2", status="failed", outputs=[{"name": "b.csv", "size": 3}]),
    ])
    refs = artifacts.artifacts_for_job("u1", job)
    names = [item["filename"] for item in refs]
    assert names == ["a.csv", "evil.sh"], "只取已完成节点的 outputs，且路径只保留文件名"
    assert all(item["artifact_id"].startswith("art_") for item in refs)


# ── 2. 声明式视图（白名单 + 有界）────────────────────────


def test_view_updated_is_declarative_and_whitelisted(monkeypatch):
    from app.office import docs as office_docs

    out_dir = office_docs.generic_outputs_dir("u1", "job-1")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.csv").write_text("name,score\nalice,90\n", encoding="utf-8")
    (out_dir / "blob.bin").write_bytes(b"\x00\x01\x02")

    table_ref = artifacts.artifact_from_output("job-1", {"name": "report.csv", "size": 20})
    table_view = views.artifact_view("u1", table_ref)
    assert table_view is not None
    assert table_view["view_type"] == "table"
    assert table_view["data"]["rows"][0] == ["name", "score"]
    assert table_view["data_ref"] == table_ref["artifact_id"]

    binary_ref = artifacts.artifact_from_output("job-1", {"name": "blob.bin", "size": 3})
    assert views.artifact_view("u1", binary_ref) is None, "不可预览的格式不下发 view_updated（前端显示降级文案）"

    # 视图类型必须在白名单内，且 payload 里没有任何脚本/HTML 字段
    from lumi_contracts.plugins.view_contribution import VIEW_TYPES

    assert table_view["view_type"] in VIEW_TYPES
    assert not {"html", "script", "iframe"} & set(table_view)


def test_timeline_view_has_no_body_text():
    steps = [
        {"id": "s1", "title": "读取文件", "status": "completed", "duration_ms": 120, "output": "SECRET-BODY"},
        {"id": "s2", "title": "写入文件", "status": "running", "duration_ms": 0},
    ]
    view = views.timeline_view(steps, job_id="job-1")
    assert view is not None and view["view_type"] == "timeline"
    blob = json.dumps(view, ensure_ascii=False)
    assert "SECRET-BODY" not in blob, "时间线视图不得携带步骤正文"
    assert [row["step_id"] for row in view["data"]["steps"]] == ["s1", "s2"]
    # 前端 viewContracts 的 timeline shape 校验要求 data.items
    assert [row["step_id"] for row in view["data"]["items"]] == ["s1", "s2"]


def test_snapshot_contributions_match_the_frontend_renderer():
    """快照投影：去掉语义不同名的 action，让前端按 view_type 判定支持与否。"""
    table = views.timeline_view([{"id": "s1", "title": "读取", "status": "completed"}], job_id="job-1")
    contributions = views.snapshot_contributions([table])
    assert contributions and "action" not in contributions[0]
    assert contributions[0]["view_type"] == "timeline"
    # 模拟前端 ViewContainer.actionOf：action || view_type 必须落在白名单里
    from lumi_contracts.plugins.view_contribution import VIEW_TYPES

    for item in contributions:
        action_of = item.get("action") or item.get("view_type") or ""
        assert action_of in VIEW_TYPES, action_of
    # 非白名单类型的快照不下发数据（前端走降级提示）
    unsafe = views.snapshot_contributions([{"view_id": "v1", "view_type": "", "data": {"script": "x"}}])
    assert unsafe[0]["data"] == {}


# ── 3. 审批事件带 node_id（既有审批接口主键）─────────────


def test_approval_event_carries_node_and_step_ids():
    from app.contracts.event_adapter import canonical_payload

    payload = canonical_payload("approval_required", {
        "node_id": "node-7",
        "step_id": "node-7",
        "capability": "workspace.write",
        "action": "workspace_write",
        "target": "README.md",
        "risk_level": "REQUIRES_APPROVAL",
    })
    assert payload["node_id"] == "node-7"
    assert payload["step_id"] == "node-7"
    assert payload["capability"] == "workspace.write"
    # 只有 step_id 时 node_id 必须兜底（前端不再需要 step_id || request_id）
    fallback = canonical_payload("approval_required", {"step_id": "s9"})
    assert fallback["node_id"] == "s9"


def test_result_side_events_dedupe_and_map_approval(monkeypatch):
    from app.services.orchestrator import Orchestrator

    job = _Job("job-1", [
        _Node("s1", outputs=[{"name": "report.csv", "size": 10}]),
        _Node("s2", metadata={"awaiting_approval": True, "risk_level": "REQUIRES_APPROVAL"}),
    ])
    from app.office import docs as office_docs

    out_dir = office_docs.generic_outputs_dir("u1", "job-1")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.csv").write_text("a,b\n", encoding="utf-8")

    emitted = (set(), set(), set())
    first = Orchestrator._result_side_events(
        job, payload_sub="u1",
        emitted_artifacts=emitted[0], emitted_views=emitted[1], emitted_approvals=emitted[2],
    )
    types = [item["type"] for item in first]
    assert types.count("artifact_created") == 1
    assert types.count("view_updated") == 1
    artifact = next(item for item in first if item["type"] == "artifact_created")
    assert artifact["artifact"]["expires_at"], "产物事件必须带过期时间（前端卡片显示）"
    assert artifact["step_id"] == "s1"
    approval = next(item for item in first if item["type"] == "approval_required")
    assert approval["node_id"] == "s2" and approval["step_id"] == "s2"

    # 同一快照再算一次：不重复下发（轮询/重连不会叠加卡片）
    second = Orchestrator._result_side_events(
        job, payload_sub="u1",
        emitted_artifacts=emitted[0], emitted_views=emitted[1], emitted_approvals=emitted[2],
    )
    assert second == []


# ── 3.1 结果归一化链：node.result → ExecutionResult → UI 投影 ──


def test_node_result_goes_through_execution_result():
    from app.contracts.node_result import execution_result_from_node
    from app.contracts.ui_projection import artifact_refs_of, ui_view
    from app.office import docs as office_docs

    out_dir = office_docs.generic_outputs_dir("u1", "job-1")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    node = _Node("s1", outputs=[{"name": "report.csv", "size": 8}])
    node.result = {"outputs": [{"name": "report.csv", "size": 8}], "content": "已生成报表", "tool": "workspace_write"}
    result = execution_result_from_node(node, container_id="job-1", user_id="u1")

    assert result.ok, "完成节点必须投影成 success 信封"
    assert result.tool_name == "workspace_write"
    assert result.node_id == "s1"
    assert result.timing.duration_ms == 500
    # 契约产物引用 + 安全展示元数据同时具备
    assert [ref.ref_id for ref in result.artifact_refs] == [artifact_refs_of(result)[0]["artifact_id"]]
    assert artifact_refs_of(result)[0]["filename"] == "report.csv"
    assert "已生成报表" in ui_view(result)["summary"]


def test_failed_node_result_projects_to_failure_envelope():
    from app.contracts.node_result import execution_result_from_node

    node = _Node("s9", status="failed")
    node.error = "工作区不可用"
    node.error_code = "WORKSPACE_DEVICE_OFFLINE"
    result = execution_result_from_node(node, container_id="job-1")
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "WORKSPACE_DEVICE_OFFLINE"


# ── 3.2 审批决议事件 ─────────────────────────────────────


def test_approval_resolution_emits_resolved_event():
    from app.services.orchestrator import Orchestrator

    node = _Node("n1", metadata={"awaiting_approval": True})
    job = _Job("job-1", [node])
    emitted: set[str] = set()
    resolved: set[str] = set()

    waiting = Orchestrator._result_side_events(
        job, payload_sub="u1", emitted_artifacts=set(), emitted_views=set(),
        emitted_approvals=emitted, resolved_approvals=resolved,
    )
    assert [item["type"] for item in waiting] == ["approval_required"]

    # 用户批准：approval_service 会移除 awaiting_approval 并写入 confirmed_tool_calls
    node.metadata = {"confirmed_tool_calls": ["fp-1"], "approval_tool": "workspace_write"}
    node.status = "pending"
    approved = Orchestrator._result_side_events(
        job, payload_sub="u1", emitted_artifacts=set(), emitted_views=set(),
        emitted_approvals=emitted, resolved_approvals=resolved,
    )
    assert [item["type"] for item in approved] == ["approval_resolved"]
    assert approved[0]["approved"] is True
    assert approved[0]["node_id"] == "n1"

    # 决议只发一次
    again = Orchestrator._result_side_events(
        job, payload_sub="u1", emitted_artifacts=set(), emitted_views=set(),
        emitted_approvals=emitted, resolved_approvals=resolved,
    )
    assert again == []


def test_rejected_approval_emits_resolved_with_reason():
    from app.services.orchestrator import Orchestrator

    node = _Node("n2", status="skipped", metadata={"awaiting_approval": True})
    node.error = "用户拒绝审批"
    job = _Job("job-1", [node])
    emitted = {"n2"}
    events = Orchestrator._result_side_events(
        job, payload_sub="u1", emitted_artifacts=set(), emitted_views=set(),
        emitted_approvals=emitted, resolved_approvals=set(),
    )
    assert [item["type"] for item in events] == ["approval_resolved"]
    assert events[0]["approved"] is False
    assert "拒绝" in events[0]["reason"]


def test_approval_still_waiting_emits_nothing_extra():
    from app.services.orchestrator import Orchestrator

    node = _Node("n3", metadata={"awaiting_approval": True})
    job = _Job("job-1", [node])
    events = Orchestrator._result_side_events(
        job, payload_sub="u1", emitted_artifacts=set(), emitted_views=set(),
        emitted_approvals={"n3"}, resolved_approvals=set(),
    )
    assert events == [], "还在等待审批时不得发决议事件"


# ── 3.3 终态：done 帧必须带真实任务状态 ───────────────────


class _FakeJobHandle:
    def __init__(self, status: str, *, final_answer: str = "", error: str = "") -> None:
        self.job_id = "job-1"
        self.conversation_id = "conv-1"
        self.created_at = 1.0
        self.updated_at = 1.0
        self.status = status
        self.error = error
        self.routing = {"execution_state": "running"}
        self.nodes = []
        self.result = {"final_answer": final_answer}


def _run_office_stream(monkeypatch, job_handle) -> tuple[list[dict], dict]:
    import asyncio

    from app.agents.orchestration.orchestrator import orchestrator as agent_orchestrator
    from app.office import stream as office_stream
    from app.services.orchestrator import Orchestrator

    async def _submit(*_args, **_kwargs):
        return job_handle

    async def _get(_job_id):
        return job_handle

    async def _read_deltas(_job_id, cursor):
        return [], cursor

    monkeypatch.setattr(agent_orchestrator, "submit_job", _submit)
    monkeypatch.setattr(agent_orchestrator, "get_job", _get)
    monkeypatch.setattr(office_stream, "read_deltas", _read_deltas)

    orch = object.__new__(Orchestrator)
    terminal: dict = {}

    async def _collect():
        return [
            event
            async for event in orch._stream_office_job(
                "u1", "conv-1", "做个文件", [], None, [], "user", None, "use_workspace_policy",
                terminal_state=terminal,
            )
        ]

    return asyncio.run(_collect()), terminal


def test_office_job_stream_reports_failed_terminal_state(monkeypatch):
    events, terminal = _run_office_stream(monkeypatch, _FakeJobHandle("failed", error="工作区不可用"))
    assert terminal["status"] == "failed"
    assert terminal["job_id"] == "job-1"
    assert terminal["error"] == "工作区不可用"
    assert events and events[0]["type"] == "job"


def test_office_job_stream_reports_completed_terminal_state(monkeypatch):
    events, terminal = _run_office_stream(monkeypatch, _FakeJobHandle("completed", final_answer="完成"))
    assert terminal["status"] == "completed"
    assert any(event.get("type") == "delta" for event in events)


def test_terminal_state_reaches_the_control_event(monkeypatch):
    """端到端：failed 任务 → done 帧带 status → 标准事件是 control(failed)。"""
    from app.contracts.events import SseEventEncoder

    _events, terminal = _run_office_stream(monkeypatch, _FakeJobHandle("failed", error="工作区不可用"))
    done_frame = {"type": "done", "job_id": terminal["job_id"], "status": terminal["status"]}
    frames = SseEventEncoder(job_id="job-1", protocol="canonical").canonical_frames(done_frame)
    assert frames[0]["type"] == "control"
    assert frames[0]["payload"]["state"] == "failed"


# ── 4. 断线续传：按 seq 补拉 ─────────────────────────────


async def _replay(monkeypatch) -> tuple[list[dict[str, Any]], _FakeRedis]:
    redis = _FakeRedis()
    _install_fake_redis(monkeypatch, redis)
    encoder = SseEventEncoder(job_id="job-1", protocol="canonical")
    recorder = job_event_log.FrameRecorder()
    for event in (
        {"type": "delta", "content": "第一段"},
        {"type": "process", "content": "正在读取文件"},
        {"type": "delta", "content": "第二段"},
        {"type": "done", "status": "completed"},
    ):
        for frame, _line in encoder.encode_frames(event):
            if recorder.add(frame):
                await recorder.flush()
    await recorder.flush()
    frames = await job_event_log.read_frames("job-1", after_seq=0, limit=100)
    return frames, redis


def test_job_event_log_replays_after_seq(monkeypatch):
    import asyncio

    frames, redis = asyncio.run(_replay(monkeypatch))
    assert [frame["type"] for frame in frames] == ["text_delta", "process", "text_delta", "control", "done"]
    assert [frame["seq"] for frame in frames] == [1, 2, 3, 4, 5]
    assert job_event_log.last_seq_of(frames) == 5
    assert redis.expires, "事件日志必须带 TTL"

    # 增量补拉：只回 after_seq 之后的事件（前端传自己的 lastSeq）
    tail = asyncio.run(job_event_log.read_frames("job-1", after_seq=3, limit=100))
    assert [frame["seq"] for frame in tail] == [4, 5]
    assert tail[0]["type"] == "control" and tail[0]["payload"]["state"] == "completed"
    assert asyncio.run(job_event_log.read_frames("job-1", after_seq=99, limit=100)) == []


def test_job_event_log_skips_frames_without_job_id(monkeypatch):
    import asyncio

    redis = _FakeRedis()
    _install_fake_redis(monkeypatch, redis)
    encoder = SseEventEncoder(conversation_id="c1", protocol="canonical")
    frames = encoder.canonical_frames({"type": "delta", "content": "闲聊"})
    assert asyncio.run(job_event_log.record_frames(frames)) == 0
    assert redis.lists == {}, "普通闲聊（无 job_id）不落任务事件日志"


def test_job_event_log_degrades_when_redis_is_down(monkeypatch):
    import asyncio

    def _boom():
        raise RuntimeError("redis down")

    import app.core.redis as redis_module

    monkeypatch.setattr(redis_module, "get_redis", _boom)
    assert asyncio.run(job_event_log.record_frames([{"type": "process", "job_id": "job-1", "seq": 1}])) == 0
    assert asyncio.run(job_event_log.read_frames("job-1", after_seq=0)) == []


def test_replay_endpoint_is_registered():
    from app.api.v1 import agents as agents_module
    from app.api.v1 import artifacts as artifacts_module

    agent_paths = {route.path for route in agents_module.router.routes}
    artifact_paths = {route.path for route in artifacts_module.router.routes}
    assert "/jobs/{job_id}/events" in agent_paths
    assert "/{artifact_id}" in artifact_paths
    assert "/{artifact_id}/download" in artifact_paths
