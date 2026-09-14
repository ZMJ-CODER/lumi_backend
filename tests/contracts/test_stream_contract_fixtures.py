"""事件契约 Fixture（方案 §8 阶段 2）+ 双协议一致性（阶段 6）+ 安全验收（阶段 7）。

一次跑通四件事：

1. **离线 Fixture**：9 个场景（普通聊天/工具/审批/产物/失败/取消/乱序重复/未知事件/视图）
   不依赖真实模型，全部走"内部事件 → 两种投影"，并把生成的 SSE 帧写进
   ``docs/fixtures/stream-events/*.json`` 供前端直接消费；
2. **终态封印**：取消场景里终态之后的内容帧必须被吞掉（后端闸门）；
3. **双协议 ViewModel 一致**：``legacy`` 与 ``canonical`` 消费同一批内部事件后，
   :meth:`StreamViewModel.snapshot` 必须逐字段一致（切换协议不改前端行为）；
4. **安全验收**：任何公开帧里不得出现原始思维链/参数/结果/凭据/绝对路径/堆栈。

Fixture 文件由本测试生成（``STREAM_FIXTURE_DIR``），因此不会与实现漂移：
期望值写在这里，帧直接落盘给前端用。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from app.contracts.events import SseEventEncoder
from app.contracts.stream_view_model import consume_frames, normalize_frames
from app.services.job_event_seal import StreamSeal

FIXTURE_DIR = Path(os.environ.get("STREAM_FIXTURE_DIR", "docs/fixtures/stream-events"))

#: 安全验收：这些字符串绝不允许出现在任何公开帧里。
FORBIDDEN_SUBSTRINGS = (
    "SECRET-COT",
    "SECRET-PROMPT",
    "SECRET-RAW",
    "sk-live-SECRET",
    "reasoning_content",
    "chain_of_thought",
    "D:\\\\Users\\\\me\\\\secret",
    "Traceback (most recent call last)",
)

#: 每个场景的脏字段：确认它们被结构性剔除（而不是"恰好没写"）。
DIRTY = {
    "reasoning_content": "SECRET-COT-不要外发",
    "chain_of_thought": "SECRET-COT-2",
    "thought_delta": "SECRET-COT-3",
    "arguments": {"path": "D:\\\\Users\\\\me\\\\secret\\\\config.py"},
    "messages": [{"role": "system", "content": "SECRET-PROMPT"}],
    "api_key": "sk-live-SECRET",
    "raw_result": "SECRET-RAW-RESULT",
    "stack": "Traceback (most recent call last): boom",
}


def _event(event_type: str, **extra) -> dict:
    return {"type": event_type, "job_id": "job-1", "conversation_id": "conv-1", **DIRTY, **extra}


SCENARIOS: list[dict] = [
    {
        "scenario": "chat_simple",
        "description": "普通聊天：text_delta × N → done",
        "input_events": [
            _event("delta", content="## 总体概况\n", message_id="m1"),
            _event("delta", content="第一段", message_id="m1"),
            _event("delta", content="，第二段。", message_id="m1"),
            _event("done", message_id="m1", status="completed"),
        ],
        "expect": {
            "canonical_types": ["text_delta", "text_delta", "text_delta", "control", "done"],
            "legacy_types": ["delta", "delta", "delta", "done"],
            "terminal_state": "completed",
            "answer_text": "## 总体概况\n第一段，第二段。",
            "dropped_after_terminal": 0,
            "artifacts": 0,
            "approvals": 0,
        },
    },
    {
        "scenario": "tool_task",
        "description": "工具任务：step_started → process → step_completed → text_delta → done",
        "input_events": [
            _event("step_started", step_id="s1", title="读取文件", tool_name="workspace_read"),
            _event("process", entry_id="p1", kind="read", title="读取文件", summary="正在读取 config.py"),
            _event(
                "step_completed",
                step_id="s1",
                status="completed",
                title="读取文件",
                duration_ms=120,
                result_summary="已读取 42 行",
            ),
            _event("delta", content="读完了。"),
            _event("done", status="completed"),
        ],
        "expect": {
            "canonical_types": [
                "step_started", "process", "step_completed", "text_delta", "control", "done",
            ],
            "legacy_types": ["step_started", "process", "step_completed", "delta", "done"],
            "terminal_state": "completed",
            "answer_text": "读完了。",
            "dropped_after_terminal": 0,
            "step_status": {"s1": "completed"},
            "process_entries": 1,
        },
    },
    {
        "scenario": "approval_task",
        "description": "审批任务：step_started → approval_required → control(waiting) → approval_resolved → step_completed → done",
        "input_events": [
            _event("step_started", step_id="s2", title="覆盖文件", tool_name="workspace_write"),
            _event(
                "approval_required",
                step_id="s2",
                node_id="n2",
                request_id="req-2",
                capability="workspace_write",
                action="workspace_write",
                target="src/app.py",
                risk_level="high",
            ),
            _event("control", state="waiting_approval", step_id="s2"),
            _event("approval_resolved", step_id="s2", node_id="n2", request_id="req-2", approved=True),
            _event("step_completed", step_id="s2", status="completed", title="覆盖文件", result_summary="已写入"),
            _event("done", status="completed"),
        ],
        "expect": {
            "canonical_types": [
                "step_started", "approval_required", "control", "approval_resolved",
                "step_completed", "control", "done",
            ],
            "legacy_types": [
                "step_started", "approval_required", "control", "approval_resolved",
                "step_completed", "done",
            ],
            "terminal_state": "completed",
            "approvals": 1,
            "approval_approved": True,
            "dropped_after_terminal": 0,
        },
    },
    {
        "scenario": "artifact_task",
        "description": "产物任务：step_started → artifact_created → step_completed → done",
        "input_events": [
            _event("step_started", step_id="s3", title="生成报告", tool_name="export_report"),
            _event(
                "artifact_created",
                artifact={
                    "artifact_id": "art-1",
                    "type": "markdown",
                    "filename": "report.md",
                    "mime_type": "text/markdown",
                    "size_bytes": 2048,
                    "expires_at": "2026-01-01T00:00:00+00:00",
                },
            ),
            _event("step_completed", step_id="s3", status="completed", title="生成报告", result_summary="报告已生成"),
            _event("done", status="completed"),
        ],
        "expect": {
            "canonical_types": ["step_started", "artifact_created", "step_completed", "control", "done"],
            "legacy_types": ["step_started", "artifact_created", "step_completed", "done"],
            "terminal_state": "completed",
            "artifacts": 1,
            "artifact_id": "art-1",
            # 事件里只给引用与元数据，绝不带下载地址/令牌
            "artifact_forbidden_keys": ["url", "download_url", "token", "path"],
            "dropped_after_terminal": 0,
        },
    },
    {
        "scenario": "view_task",
        "description": "视图任务：小数据直发；超限数据只发 data_ref（不再搬运正文）",
        "input_events": [
            _event("step_started", step_id="s4", title="查询", tool_name="query", summary="正在执行：查询"),
            _event(
                "view_updated",
                view={"view_id": "v1", "view_type": "table", "title": "结果", "data": {"rows": [{"a": 1}]}},
            ),
            _event(
                "view_updated",
                view={
                    "view_id": "v2",
                    "view_type": "table",
                    "title": "大结果",
                    "data": {"rows": [{"i": index} for index in range(1005)]},
                },
            ),
            _event("done", status="completed"),
        ],
        "expect": {
            "canonical_types": ["step_started", "view_updated", "view_updated", "control", "done"],
            "legacy_types": ["step_started", "view_updated", "view_updated", "done"],
            "terminal_state": "completed",
            "views": 2,
            "view_truncated": {"v1": False, "v2": True},
            "dropped_after_terminal": 0,
        },
    },
    {
        "scenario": "failure_task",
        "description": "失败任务：task_failed → control(failed) + error(统一错误码)",
        "input_events": [
            _event("step_started", step_id="s5", title="写文件", tool_name="workspace_write", summary="正在执行：写文件"),
            _event(
                "task_failed",
                step_id="s5",
                error_code="MCP_UNAVAILABLE",
                message="客户端未连接",
                status="failed",
            ),
        ],
        "expect": {
            "canonical_types": ["step_started", "control", "error"],
            "legacy_types": ["step_started", "task_failed"],
            "terminal_state": "failed",
            "error_codes": ["CAPABILITY_UNAVAILABLE"],
            "error_safe_message_nonempty": True,
            "dropped_after_terminal": 0,
        },
    },
    {
        "scenario": "cancel_task",
        "description": "取消任务：终态封印——cancel 后迟到的 text_delta/step_completed 不改变状态",
        "input_events": [
            _event("step_started", step_id="s6", title="长任务", tool_name="long_task", summary="正在执行：长任务"),
            _event("delta", content="正在处理…"),
            _event("cancelled", status="cancelled", reason_code="USER_CANCELLED"),
            # ↓ 迟到帧：取消已经受理，这些必须被吞掉
            _event("delta", content="迟到正文不应出现"),
            _event("step_completed", step_id="s6", status="completed", result_summary="迟到完成"),
        ],
        "expect": {
            "canonical_types": ["step_started", "text_delta", "control", "done"],
            "legacy_types": ["step_started", "delta", "cancelled"],
            "terminal_state": "cancelled",
            "answer_text": "正在处理…",
            "dropped_after_terminal": 2,
            "absent_text": ["迟到正文不应出现", "迟到完成"],
        },
    },
    {
        "scenario": "out_of_order_duplicate",
        "description": "乱序 + 重复：按 seq 排序、按 event_id 去重，日志不重复",
        "frames_only": True,
        "input_events": [
            {"event_id": "e3", "seq": 3, "type": "done", "job_id": "job-1", "payload": {"state": "completed"}},
            {"event_id": "e1", "seq": 1, "type": "text_delta", "job_id": "job-1", "payload": {"content": "你好"}},
            {"event_id": "e1", "seq": 1, "type": "text_delta", "job_id": "job-1", "payload": {"content": "你好"}},
            {"event_id": "e2", "seq": 2, "type": "process", "job_id": "job-1",
             "payload": {"entry_id": "p9", "kind": "read", "title": "读取", "summary": "正在读取"}},
            {"event_id": "e2", "seq": 2, "type": "process", "job_id": "job-1",
             "payload": {"entry_id": "p9", "kind": "read", "title": "读取", "summary": "正在读取"}},
        ],
        "expect": {
            "normalized_types": ["text_delta", "process", "done"],
            "terminal_state": "completed",
            "answer_text": "你好",
            "process_entries": 1,
            "dropped_after_terminal": 0,
        },
    },
    {
        "scenario": "unknown_event",
        "description": "未知事件：有安全字段则透传并打 unsupported 标记；看不懂的结构不解析",
        "input_events": [
            _event("text_delta", content="正常内容"),
            {"type": "future_event_from_newer_backend", "job_id": "job-1", "capability": "x", "brand_new": {"a": 1}},
            {"type": "opaque_future_event", "job_id": "job-1", "brand_new_blob": {"deep": "y" * 5000}},
            _event("done", status="completed"),
        ],
        "expect": {
            "canonical_types": ["text_delta", "future_event_from_newer_backend", "opaque_future_event", "control", "done"],
            "legacy_types": ["text_delta", "future_event_from_newer_backend", "opaque_future_event", "done"],
            "terminal_state": "completed",
            "unsupported": ["future_event_from_newer_backend", "opaque_future_event"],
            "unknown_marked": True,
            "opaque_ref_prefix": "opaque:opaque_future_event:",
            "dropped_after_terminal": 0,
        },
    },
]

SCENARIOS_BY_NAME = {item["scenario"]: item for item in SCENARIOS}


def _project(scenario: dict, protocol: str) -> tuple[list[dict], int]:
    """内部事件 → 帧（含终态封印），返回 ``(帧列表, 被吞掉的帧数)``。"""
    if scenario.get("frames_only"):
        return [dict(frame) for frame in scenario["input_events"]], 0
    encoder = SseEventEncoder(
        job_id="job-1", conversation_id="conv-1", protocol=protocol, start_seq=0
    )
    seal = StreamSeal()
    frames: list[dict] = []
    dropped = 0
    for event in scenario["input_events"]:
        produced = [frame for frame, _line in encoder.encode_frames(event)]
        kept, count = seal.filter(produced)
        frames.extend(kept)
        dropped += count
    return _pin_frame_identity(frames, scenario["scenario"], protocol), dropped


def _pin_frame_identity(frames: list[dict], scenario: str, protocol: str) -> list[dict]:
    """把 ``event_id`` / ``occurred_at`` 钉成确定值，保证 Fixture 文件**逐字节稳定**。

    真实运行时 ``event_id`` 是内容哈希（重连/补拉后同一逻辑事件同 id）、
    ``occurred_at`` 是当前时间；但落盘的联调 Fixture 必须稳定，否则每次跑测试
    都会改一遍仓库文件（脏工作区、diff 噪声、CI "uncommitted changes" 误报）。
    去重语义由 ``out_of_order_duplicate`` 场景与后端测试单独覆盖。
    """
    pinned: list[dict] = []
    for index, frame in enumerate(frames, start=1):
        item = dict(frame)
        seq = item.get("seq") or index
        item["event_id"] = f"evt_{scenario}-{protocol}-{seq}"
        item["occurred_at"] = f"2026-01-01T00:00:{int(seq) % 60:02d}.000Z"
        pinned.append(item)
    return pinned


def _dump(frames: list[dict]) -> str:
    return json.dumps(frames, ensure_ascii=False, default=str)


def _assert_no_forbidden(frames: list[dict]) -> None:
    blob = _dump(frames)
    for marker in FORBIDDEN_SUBSTRINGS:
        assert marker not in blob, f"公开帧里出现了内部信息：{marker}"


# ── 1. Fixture 全场景 ────────────────────────────────────


def test_all_fixtures_project_and_match_expectations(tmp_path, monkeypatch):
    if not FIXTURE_DIR.exists():
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("STREAM_FIXTURE_DIR", str(FIXTURE_DIR))

    for scenario in SCENARIOS:
        name = scenario["scenario"]
        expect = scenario["expect"]
        legacy, legacy_dropped = _project(scenario, "legacy")
        canonical, canonical_dropped = _project(scenario, "canonical")
        _assert_no_forbidden(legacy)
        _assert_no_forbidden(canonical)

        if "legacy_types" in expect:
            assert [frame["type"] for frame in legacy] == expect["legacy_types"], name
        if "canonical_types" in expect:
            assert [frame["type"] for frame in canonical] == expect["canonical_types"], name
        if "normalized_types" in expect:
            assert [frame["type"] for frame in normalize_frames(canonical)] == expect["normalized_types"], name
        if "dropped_after_terminal" in expect:
            assert legacy_dropped == expect["dropped_after_terminal"], f"{name}: 后端封印未生效"
            assert canonical_dropped == expect["dropped_after_terminal"], f"{name}: 后端封印未生效"

        legacy_vm = consume_frames(legacy)
        canonical_vm = consume_frames(canonical)
        assert legacy_vm.status == expect.get("terminal_state", legacy_vm.status), name
        assert canonical_vm.status == expect.get("terminal_state", canonical_vm.status), name
        if "answer_text" in expect:
            assert legacy_vm.answer_text == expect["answer_text"], name
            assert canonical_vm.answer_text == expect["answer_text"], name
        if "absent_text" in expect:
            assert all(text not in canonical_vm.answer_text for text in expect["absent_text"]), name
        if "artifacts" in expect:
            assert len(legacy_vm.artifacts) == expect["artifacts"] == len(canonical_vm.artifacts), name
        if "artifact_id" in expect:
            assert expect["artifact_id"] in canonical_vm.artifacts, name
        if "approvals" in expect:
            assert len(canonical_vm.approvals) == expect["approvals"], name
        if "approval_approved" in expect:
            assert list(canonical_vm.approvals.values())[0]["approved"] is expect["approval_approved"], name
        if "views" in expect:
            assert len(canonical_vm.views) == expect["views"] == len(legacy_vm.views), name
        if "view_truncated" in expect:
            for view_id, truncated in expect["view_truncated"].items():
                assert canonical_vm.views[view_id]["truncated"] is truncated, f"{name}:{view_id}"
        if "step_status" in expect:
            for step_id, status in expect["step_status"].items():
                assert canonical_vm.steps[step_id]["status"] == status, f"{name}:{step_id}"
        if "process_entries" in expect:
            assert len(canonical_vm.process_log) == expect["process_entries"], name
        if "error_codes" in expect:
            assert [item["code"] for item in canonical_vm.errors] == expect["error_codes"], name
        if "error_safe_message_nonempty" in expect:
            assert all(item["safe_message"] for item in canonical_vm.errors), name
        if "unsupported" in expect:
            assert sorted(set(canonical_vm.unsupported)) == expect["unsupported"], name

        # 落盘给前端：real frames（含 event_id/seq/occurred_at）
        payload = {
            "scenario": name,
            "description": scenario["description"],
            "input_events": scenario["input_events"],
            "legacy_frames": legacy,
            "canonical_frames": canonical,
            "expect": expect,
        }
        (FIXTURE_DIR / f"{name}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )


def test_fixture_output_is_deterministic_across_runs():
    """Fixture 必须逐字节稳定：否则每次跑测试都会改脏工作区（diff 噪声 / CI 误报）。

    ``event_id`` 与 ``occurred_at`` 在运行时是随机/当前时间，落盘前由
    :func:`_pin_frame_identity` 钉成确定值。
    """
    for scenario in SCENARIOS:
        for protocol in ("legacy", "canonical"):
            first = _project(scenario, protocol)[0]
            second = _project(scenario, protocol)[0]
            assert _dump(first) == _dump(second), f"{scenario['scenario']}/{protocol} 输出不稳定"


def test_fixture_files_are_written_for_the_frontend():
    for scenario in SCENARIOS:
        path = FIXTURE_DIR / f"{scenario['scenario']}.json"
        assert path.exists(), f"缺少前端联调 fixture：{path}"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["legacy_frames"] and data["canonical_frames"]
        # 前端只需要 type/seq/version/event_id/payload（标准投影）
        canonical = data["canonical_frames"]
        if canonical and "payload" in canonical[0]:
            assert {"type", "seq", "payload"} <= set(canonical[0])


# ── 2. 双协议 ViewModel 一致（阶段 6）────────────────────


def test_both_protocols_produce_the_same_view_model():
    """切换协议前后，前端拿到的 ViewModel 必须完全一致——不比较 JSON 字符串。"""
    for scenario in SCENARIOS:
        if scenario.get("frames_only"):
            continue
        legacy, _ = _project(scenario, "legacy")
        canonical, _ = _project(scenario, "canonical")
        legacy_snapshot = consume_frames(legacy).snapshot()
        canonical_snapshot = consume_frames(canonical).snapshot()
        assert legacy_snapshot == canonical_snapshot, (
            f"{scenario['scenario']} 双协议 ViewModel 不一致："
            f"{ {key: (legacy_snapshot[key], canonical_snapshot[key]) for key in legacy_snapshot if legacy_snapshot[key] != canonical_snapshot[key]} }"
        )


def test_view_model_dedupes_and_sorts_like_the_frontend_must():
    scenario = SCENARIOS_BY_NAME["out_of_order_duplicate"]
    frames = scenario["input_events"]
    normalized = normalize_frames(frames)
    assert [frame["seq"] for frame in normalized] == [1, 2, 3], "按 seq 排序"
    assert len(normalized) == 3, "重复 event_id 去重"
    vm = consume_frames(frames)
    assert vm.answer_text == "你好"
    assert len(vm.process_log) == 1
    assert vm.status == "completed"


# ── 3. 安全验收（阶段 7 / 验收清单 #9）───────────────────


def test_public_frames_never_carry_internal_fields():
    """逐帧检查：内部字段在公开事件里零出现（标准投影与旧投影都查）。"""
    forbidden_keys = {
        "reasoning", "reasoning_content", "chain_of_thought", "thought", "thought_delta",
        "arguments", "messages", "prompt", "system_prompt", "api_key", "token",
        "raw_result", "raw_output", "stack", "security_context", "audit_hash",
        "sanitizer_version", "source",
        # 说明：``target`` 不在禁入清单里——方案 §2.3 的 ``approval_required``
        # 载荷明确要求带 ``target``（审批对象）。§1.2 禁的是把 ``target`` 用成
        # "内部组件标识"，不是审批目标本身。
    }
    for scenario in SCENARIOS:
        for protocol in ("legacy", "canonical"):
            frames, _ = _project(scenario, protocol)
            for frame in frames:
                payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else frame
                leaked = forbidden_keys & set(payload)
                assert not leaked, f"{scenario['scenario']}/{protocol} 泄露内部字段：{leaked}"


def test_terminal_frame_is_never_followed_by_content_frames():
    """验收清单 #2（后端侧）：终态之后不得再有内容类帧。"""
    for scenario in SCENARIOS:
        if scenario.get("frames_only"):
            continue
        for protocol in ("legacy", "canonical"):
            frames, _ = _project(scenario, protocol)
            seen_terminal = False
            for frame in frames:
                event_type = str(frame.get("type") or "")
                if seen_terminal:
                    assert event_type not in {
                        "text_delta", "delta", "process", "step_started", "step_completed",
                        "artifact_created", "view_updated", "approval_required",
                    }, f"{scenario['scenario']}/{protocol}: 终态后仍有 {event_type}"
                if event_type in {"done", "task_completed", "task_failed", "cancelled"} or (
                    event_type == "control"
                    and str((frame.get("payload") or {}).get("state") or "") in
                    {"completed", "failed", "cancelled", "interrupted", "blocked"}
                ):
                    seen_terminal = True


def test_cancel_scenario_keeps_status_after_late_frames():
    """验收清单 #2：取消后迟到事件不影响状态、不重复日志、不回跳。

    后端闸门（封印）已经把迟到帧拦在出口；前端闸门由
    :func:`test_frontend_terminal_drop_rule` 单独验证。
    """
    scenario = SCENARIOS_BY_NAME["cancel_task"]
    canonical, dropped = _project(scenario, "canonical")
    assert dropped == 2, "后端封印必须吞掉取消后迟到的内容帧"
    vm = consume_frames(canonical)
    assert vm.status == "cancelled"
    assert vm.answer_text == "正在处理…"
    assert vm.dropped_after_terminal == 0, "迟到帧根本没到前端，自然不需要前端再丢"
    assert not any("迟到" in json.dumps(frame, ensure_ascii=False) for frame in canonical)


def test_frontend_terminal_drop_rule():
    """前端闸门（方案 §7.3 #3）：即使迟到帧漏到客户端，终态后也必须丢弃。"""
    scenario = SCENARIOS_BY_NAME["cancel_task"]
    canonical, _ = _project(scenario, "canonical")
    # 人为把两条迟到帧塞回去，模拟"后端封印失效/多路复用交错"的极端情况
    late = [
        {"event_id": "late-1", "seq": 90, "type": "text_delta", "job_id": "job-1",
         "payload": {"content": "迟到正文不应出现"}},
        {"event_id": "late-2", "seq": 91, "type": "step_completed", "job_id": "job-1",
         "payload": {"step_id": "s6", "status": "completed", "output_summary": "迟到完成"}},
    ]
    vm = consume_frames([*canonical, *late])
    assert vm.status == "cancelled", "状态不回跳"
    assert vm.dropped_after_terminal == 2
    assert "迟到正文不应出现" not in vm.answer_text
    assert vm.steps["s6"]["status"] != "completed"


# ── 4. 前端冻结契约对齐（前端仓库只读，这里以常量的形式钉住） ──────────
#
# 来源（E:\javaidea\lumi，只读参照）：
#   src/services/streamConsumer.js::STREAM_EVENT_VERSION
#   electron/stream-fixtures.cases.cjs::{public_event_fields,artifact_signing,error_code_expectations,view_limits}
# 这些常量一旦变化，前端会**静默降级/丢弃**事件，因此在这里做后端侧回归。

#: 前端支持的最高协议版本：``version`` 大于它的帧会被 `UNSUPPORTED_VERSION` 丢弃。
FRONTEND_SUPPORTED_STREAM_VERSION = 1

#: 前端冻结的 12 个错误码文案表（键集合必须与后端冻结码一致）。
FRONTEND_FROZEN_ERROR_CODES = {
    "TARGET_REQUIRED", "DEPENDENCY_MISSING_WORKSPACE", "CAPABILITY_UNAVAILABLE",
    "PROVIDER_UNHEALTHY", "PERMISSION_DENIED", "TOOL_NOT_REGISTERED",
    "APPROVAL_REQUIRED", "SECURITY_BLOCKED", "PLUGIN_RESOURCE_EXCEEDED",
    "PLUGIN_UNINSTALLED", "RESULT_REF_EXPIRED", "SYSTEM_CANCELLED",
}

#: 前端冻结的公开事件字段（``schema_version`` 已在前端白名单里）。
FRONTEND_PUBLIC_EVENT_FIELDS = {
    "event_id", "version", "seq", "type", "trace_id", "conversation_id",
    "job_id", "occurred_at", "payload", "schema_version",
}

#: 前端执行的视图上限（后端必须**在事件流里**执行，因为前端不重复判断）。
FRONTEND_VIEW_LIMITS = {"max_bytes": 65_536, "max_depth": 10, "max_elements": 1_000}


def test_canonical_frames_stay_within_the_frontend_supported_version():
    """协议版本必须 ≤ 前端支持版本：否则前端整条流走 UNSUPPORTED_VERSION 降级。"""
    from lumi_contracts import build_envelope

    frame = build_envelope("delta", {"content": "hi"}, job_id="job-1").to_canonical_frame()
    assert frame["version"] <= FRONTEND_SUPPORTED_STREAM_VERSION
    for scenario in SCENARIOS:
        if scenario.get("frames_only"):
            continue
        canonical, _ = _project(scenario, "canonical")
        for item in canonical:
            assert int(item["version"]) <= FRONTEND_SUPPORTED_STREAM_VERSION, scenario["scenario"]
            assert {"type", "seq", "job_id", "payload"} <= set(item)


def test_canonical_frame_fields_match_the_frontend_whitelist():
    """标准帧字段必须是前端认识的集合（多出来的会进 UNKNOWN 分支）。"""
    canonical, _ = _project(SCENARIOS_BY_NAME["chat_simple"], "canonical")
    for frame in canonical:
        assert set(frame) <= FRONTEND_PUBLIC_EVENT_FIELDS, set(frame) - FRONTEND_PUBLIC_EVENT_FIELDS


def test_frozen_error_codes_match_the_frontend_table():
    from lumi_contracts.events.errors import FROZEN_ERROR_CODES

    assert FROZEN_ERROR_CODES == FRONTEND_FROZEN_ERROR_CODES


def test_view_limits_match_the_frontend_contract():
    from lumi_contracts.events.envelope import (
        VIEW_EVENT_DATA_MAX_BYTES,
        VIEW_EVENT_DATA_MAX_DEPTH,
        VIEW_EVENT_DATA_MAX_ITEMS,
    )

    assert VIEW_EVENT_DATA_MAX_BYTES == FRONTEND_VIEW_LIMITS["max_bytes"]
    assert VIEW_EVENT_DATA_MAX_DEPTH == FRONTEND_VIEW_LIMITS["max_depth"]
    assert VIEW_EVENT_DATA_MAX_ITEMS == FRONTEND_VIEW_LIMITS["max_elements"]


def test_error_payload_carries_the_field_names_the_frontend_reads():
    """前端读 ``safe_message`` / ``safe_next_action`` / ``detail_ref`` / ``category``。"""
    from lumi_contracts import build_payload

    payload = build_payload("error", {"code": "CAPABILITY_UNAVAILABLE"})
    for key in ("code", "category", "retryable", "safe_message", "detail_ref", "safe_next_action"):
        assert key in payload, key
    control = build_payload("control", {"state": "failed", "error_code": "PERMISSION_DENIED"})
    assert control["error_code"] == "PERMISSION_DENIED"
    assert control["safe_next_action"]


def test_replay_endpoint_reports_protocol_and_version_honestly(monkeypatch):
    """补拉接口的 ``protocol`` / ``version`` 必须与帧形状一致（前端按此解析）。"""
    import asyncio

    import app.core.redis as redis_module

    from app.api.v1 import agents as agents_api
    from app.services import job_event_log

    class _ReplayRedis:
        def __init__(self) -> None:
            self.lists: dict[str, list[str]] = {}

        async def rpush(self, key: str, *values: str) -> int:
            self.lists.setdefault(key, []).extend(values)
            return len(self.lists[key])

        async def ltrim(self, key: str, start: int, end: int) -> bool:
            rows = self.lists.get(key, [])
            self.lists[key] = rows[start:] if end == -1 else rows[start : end + 1]
            return True

        async def expire(self, key: str, seconds: int) -> bool:
            return True

        async def lrange(self, key: str, start: int, end: int) -> list[str]:
            rows = self.lists.get(key, [])
            return rows[start:] if end == -1 else rows[start : end + 1]

        async def get(self, key: str):
            return None

        async def set(self, key: str, value: str, ex: int | None = None):
            return True

        async def delete(self, *keys: str):
            return len(keys)

    fake_redis = _ReplayRedis()
    monkeypatch.setattr(redis_module, "get_redis", lambda: fake_redis)

    async def _owned(job_id: str, user_id: str):
        return None

    monkeypatch.setattr(agents_api, "_get_owned_job", _owned)
    canonical = _project(SCENARIOS_BY_NAME["chat_simple"], "canonical")[0]

    async def main():
        await job_event_log.record_frames(canonical)
        return await agents_api.replay_agent_job_events(
            "job-1", after_seq=0, limit=500, payload={"sub": "u1"}
        )

    data = asyncio.run(main())["data"]
    assert data["protocol"] == "canonical"
    assert data["version"] <= FRONTEND_SUPPORTED_STREAM_VERSION
    assert data["count"] == len(canonical) and data["events"]
    assert all("payload" in frame for frame in data["events"])




