"""快照膨胀三条硬规则（方案 §5.3）回归：200 条 / 单条 2KB / 快照 256KB。

为什么现在必须做对：这三条是**写入路径**规则，晚做要清洗历史数据。
"""

from __future__ import annotations

from lumi_contracts import JobRunView, ProcessLogEntry, StepView
from lumi_contracts.persistence.run_view import (
    PROCESS_LOG_ENTRY_MAX_BYTES,
    PROCESS_LOG_MAX_ENTRIES,
    SNAPSHOT_MAX_BYTES,
    clip_utf8,
)


def _entry(index: int, **overrides) -> ProcessLogEntry:
    base = {
        "entry_id": f"e{index}",
        "kind": "read",
        "title": f"步骤 {index}",
        "summary": f"摘要 {index}",
        "status": "completed",
        "sequence": index,
    }
    base.update(overrides)
    return ProcessLogEntry(**base)


# ── 硬规则②：单条 ≤ 2KB ──────────────────────────────────


def test_clip_utf8_is_byte_safe():
    text = "中文" * 2000
    clipped = clip_utf8(text, 100)
    assert len(clipped.encode("utf-8")) <= 100
    assert clipped.endswith("…")
    # 不被切坏：能正常编码/解码
    assert clipped.encode("utf-8").decode("utf-8") == clipped
    assert clip_utf8("短文本") == "短文本"
    assert clip_utf8("") == ""


def test_process_log_entries_are_byte_bounded_in_snapshot():
    view = JobRunView(job_id="job-1").with_process_log([
        _entry(1, summary="很长的摘要" * 1500, detail="细节" * 1500),
    ])
    entry = view.process_log[0]
    assert len(entry.summary.encode("utf-8")) <= PROCESS_LOG_ENTRY_MAX_BYTES
    assert len(entry.detail.encode("utf-8")) <= PROCESS_LOG_ENTRY_MAX_BYTES
    assert entry.summary.endswith("…")


# ── 硬规则①：日志 200 条 + 归档引用 ──────────────────────


def test_with_process_log_keeps_only_the_recent_window():
    view = JobRunView(job_id="job-1").with_process_log([_entry(i) for i in range(300)])
    assert len(view.process_log) == PROCESS_LOG_MAX_ENTRIES
    assert view.process_log[-1].entry_id == "e299", "保留最近的一段"


def test_roll_process_log_returns_overflow_for_archiving():
    view = JobRunView(job_id="job-1")
    view, archived = view.roll_process_log([_entry(i) for i in range(260)])
    assert len(view.process_log) == PROCESS_LOG_MAX_ENTRIES
    assert len(archived) == 60
    assert archived[0].entry_id == "e0" and archived[-1].entry_id == "e59"
    assert view.log_archive_count == 60

    view = view.with_log_archive("artifact:log-archive-1")
    assert view.log_archive_ref == "artifact:log-archive-1"
    # 再滚一轮：归档条数累加，窗口始终有界
    view, archived_again = view.roll_process_log([_entry(i) for i in range(260, 300)])
    assert len(view.process_log) == PROCESS_LOG_MAX_ENTRIES
    assert view.log_archive_count == 60 + len(archived_again)
    snapshot = view.to_snapshot()
    assert snapshot["log_archive_ref"] == "artifact:log-archive-1"
    assert snapshot["log_archive_count"] == view.log_archive_count


def test_roll_process_log_within_window_archives_nothing():
    view, archived = JobRunView(job_id="job-1").roll_process_log([_entry(i) for i in range(10)])
    assert archived == []
    assert len(view.process_log) == 10
    assert view.log_archive_count == 0


# ── 硬规则③：快照 > 256KB 告警 + 强制收缩 ────────────────


def _fat_view() -> JobRunView:
    steps = [
        StepView(id=f"s{i}", title="步骤标题" * 20, status="completed", output="输出内容" * 400)
        for i in range(60)
    ]
    view = JobRunView(job_id="job-fat", steps=steps)
    return view.with_process_log([
        _entry(i, summary="摘要" * 300, detail="细节" * 300) for i in range(200)
    ])


def test_fat_snapshot_is_measured_and_shrunk_to_the_limit():
    view = _fat_view()
    raw_size = view.snapshot_size_bytes()
    assert raw_size > SNAPSHOT_MAX_BYTES, f"构造的快照应当超限（当前 {raw_size}B）"

    snapshot = view.to_snapshot()  # 写入路径唯一入口：自动收缩
    from lumi_contracts.persistence.run_view import _dump_size

    assert _dump_size(snapshot) <= SNAPSHOT_MAX_BYTES
    assert snapshot["truncated"] is True, "收缩必须留下标记（前端/排障要知道）"
    # 老步骤只留 status/error_code/result_ref：展示字段被丢掉
    assert snapshot["steps"][0]["output"] == ""
    # 过程日志窗口有界
    assert len(snapshot["process_log"]) <= 50


def test_small_snapshot_is_untouched():
    view = JobRunView(job_id="job-small", steps=[StepView(id="s1", title="标题", output="ok")])
    view = view.with_process_log([_entry(1)])
    snapshot = view.to_snapshot()
    assert snapshot["truncated"] is False
    assert snapshot["steps"][0]["output"] == "ok"
    assert len(snapshot["process_log"]) == 1
    assert snapshot["job_id"] == "job-small"


def test_shrink_is_idempotent_and_keeps_recovery_fields():
    view = _fat_view()
    once = view.shrink()
    twice = once.shrink()
    assert once.snapshot_size_bytes() == twice.snapshot_size_bytes()
    # 恢复所需字段仍在（前端刷新靠这些）
    for step in twice.to_snapshot()["steps"]:
        assert "status" in step and "id" in step


def test_snapshot_includes_the_new_bound_fields():
    snapshot = JobRunView(job_id="job-1").to_snapshot()
    assert snapshot["log_archive_ref"] == ""
    assert snapshot["log_archive_count"] == 0
    assert snapshot["truncated"] is False
