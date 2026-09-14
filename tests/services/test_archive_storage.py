"""阶段 3：过程日志溢出的真实归档（``ARCHIVE_CONTENT_V2``）。

覆盖：

* 开关关闭 → 纯空操作（不写文件、不加 routing 字段、run_view 形状不变）；
* 窗口溢出 → 溢出条目落成真实产物 + ``with_log_archive`` 引用（``log_archive_ref`` 非空）；
* 该引用能通过冻结的 ``GET /api/v1/artifacts/{ref}/content`` 读回内容（真实存储，不是占位符）；
* 保留策略来自 settings（运行中任务 / 已完成任务 / 用户另存产物三类，代码不写死）；
* 未溢出时什么都不做，且重复归档幂等（不重复条目）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.agents.orchestration.models import Job, JobStatus
from app.api.v1 import agents as agents_api
from app.api.v1 import artifacts as artifacts_api
from app.contracts.process_log import merge_job_process_log, process_log_payload
from app.core.config import settings
from app.services import artifacts
from app.services import process_log_archive
from lumi_contracts.persistence.run_view import PROCESS_LOG_MAX_ENTRIES


def _job_with_steps(
    count: int,
    *,
    status: JobStatus = JobStatus.COMPLETED,
    user_id: str = "u1",
    job_id: str = "job-archive-1",
) -> Job:
    steps = [
        {"id": f"s{index}", "title": f"步骤 {index}", "status": "completed"}
        for index in range(count)
    ]
    return Job(
        job_id=job_id,
        user_id=user_id,
        user_role="user",
        request="归档测试",
        scene="office",
        status=status,
        routing={
            "execution_mode": "step_confirm",
            "execution_state": "completed",
            "plan_revision": 1,
            "current_step_index": count,
            "steps": steps,
        },
    )


def _overflow_size(job: Job) -> int:
    """当前全量过程日志里应当被归档的条数（窗口外），与实现同源。"""
    total = len(process_log_payload(merge_job_process_log(job, limit=0)))
    return max(0, total - PROCESS_LOG_MAX_ENTRIES)


def _enable_archive(monkeypatch, tmp_path: Path, *, on: bool = True) -> None:
    monkeypatch.setattr(settings, "ARCHIVE_CONTENT_V2", on)
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))


class _OrchestratorStub:
    def __init__(self, job: Job) -> None:
        self._job = job

    async def get_job(self, job_id: str) -> Job | None:
        return self._job if job_id == self._job.job_id else None


def _run_view_of(monkeypatch, job: Job) -> dict:
    monkeypatch.setattr(agents_api, "orchestrator", _OrchestratorStub(job))
    return asyncio.run(agents_api.get_agent_job(job.job_id, {"sub": job.user_id}))["data"]["run_view"]


# ── 开关关闭：行为与改造前一致 ───────────────────────────────


def test_flag_off_is_a_no_op(monkeypatch, tmp_path):
    _enable_archive(monkeypatch, tmp_path, on=False)
    job = _job_with_steps(PROCESS_LOG_MAX_ENTRIES + 60)

    assert process_log_archive.archive_process_log_overflow(job) is None
    assert process_log_archive.ROUTING_ARCHIVE_KEY not in job.routing
    assert process_log_archive.archive_view_fields(job) == {}
    assert not process_log_archive.archive_path(job).exists(), "关闭开关不得写归档文件"
    view = _run_view_of(monkeypatch, job)
    assert "log_archive_ref" not in view and "log_archive_count" not in view


def test_within_window_archives_nothing(monkeypatch, tmp_path):
    _enable_archive(monkeypatch, tmp_path)
    job = _job_with_steps(10)
    assert process_log_archive.archive_process_log_overflow(job) is None
    view, archived = process_log_archive.roll_process_log_view(job)
    assert archived == []
    assert view.log_archive_ref == ""
    assert not process_log_archive.archive_path(job).exists()


# ── 溢出 → 真实归档 ────────────────────────────────────────


def test_overflow_is_archived_with_a_real_ref(monkeypatch, tmp_path):
    _enable_archive(monkeypatch, tmp_path)
    job = _job_with_steps(PROCESS_LOG_MAX_ENTRIES + 60)
    expected = _overflow_size(job)
    assert expected >= 60

    meta = process_log_archive.archive_process_log_overflow(job)
    assert meta is not None
    assert meta["ref"].startswith("art_")
    assert meta["count"] == expected
    assert meta["window_entries"] == PROCESS_LOG_MAX_ENTRIES
    assert meta["ref"] == job.routing[process_log_archive.ROUTING_ARCHIVE_KEY]["ref"]

    path = process_log_archive.archive_path(job)
    assert path.is_file() and path.name == process_log_archive.ARCHIVE_FILENAME
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(rows) == expected
    entry_ids = [row["entry_id"] for row in rows]
    assert "step:s0" in entry_ids and "step:s59" in entry_ids
    assert "step:s60" not in entry_ids, "窗口内的条目不得进入归档"

    # 窗口内仍保留最近 200 条（硬规则①）；把真实引用用契约 API 写回快照。
    view, archived = process_log_archive.roll_process_log_view(job)
    assert len(view.process_log) == PROCESS_LOG_MAX_ENTRIES
    assert len(archived) == expected
    assert view.log_archive_count == expected
    archived_view = view.with_log_archive(meta["ref"], count=meta["count"])
    snapshot = archived_view.to_snapshot()
    assert snapshot["log_archive_ref"] == meta["ref"]
    assert snapshot["log_archive_count"] == expected


def test_archived_content_is_readable_through_the_frozen_artifact_api(monkeypatch, tmp_path):
    _enable_archive(monkeypatch, tmp_path)
    job = _job_with_steps(PROCESS_LOG_MAX_ENTRIES + 60)
    expected = _overflow_size(job)
    meta = process_log_archive.archive_process_log_overflow(job)
    assert meta is not None

    # 产物引用出现在 run_view 里（前端只需这一个入口）
    view = _run_view_of(monkeypatch, job)
    assert view["log_archive_ref"] == meta["ref"]
    assert view["log_archive_count"] == expected

    body = asyncio.run(
        artifacts_api.get_artifact_content(
            meta["ref"],
            job_id="",
            workspace_id="",
            max_bytes=65_536,
            payload={"sub": job.user_id},
        )
    )
    assert body["code"] == 0
    content = body["data"]["content"]
    assert body["data"]["truncated"] is False
    assert "step:s0" in content and "step:s59" in content
    assert "step:s60" not in content, "窗口内的条目不应出现在归档里"
    assert str(tmp_path) not in json.dumps(body, ensure_ascii=False)


def test_archiving_is_idempotent_and_cumulative(monkeypatch, tmp_path):
    _enable_archive(monkeypatch, tmp_path)
    job = _job_with_steps(PROCESS_LOG_MAX_ENTRIES + 60)
    expected = _overflow_size(job)
    first = process_log_archive.archive_process_log_overflow(job)
    second = process_log_archive.archive_process_log_overflow(job)
    assert first is not None and second is not None
    assert second["count"] == first["count"] == expected
    path = process_log_archive.archive_path(job)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    entry_ids = [row["entry_id"] for row in rows]
    assert len(entry_ids) == len(set(entry_ids)) == expected

    # 追加更多步骤：归档窗口外的条目集合变大，但仍无重复。
    bigger = _job_with_steps(PROCESS_LOG_MAX_ENTRIES + 90)
    bigger_expected = _overflow_size(bigger)
    third = process_log_archive.archive_process_log_overflow(bigger)
    assert third is not None and third["count"] == bigger_expected > expected


def test_archive_never_leaks_server_paths_into_ref_metadata(monkeypatch, tmp_path):
    _enable_archive(monkeypatch, tmp_path)
    job = _job_with_steps(PROCESS_LOG_MAX_ENTRIES + 5)
    meta = process_log_archive.archive_process_log_overflow(job)
    assert meta is not None
    blob = json.dumps(meta, ensure_ascii=False)
    assert str(tmp_path) not in blob and "office_outputs" not in blob
    assert set(meta) == {
        "ref", "count", "filename", "size_bytes", "written_at", "expires_at", "window_entries",
        "category", "retention_setting", "retention_seconds", "effective_seconds", "clamped",
        # 每个产物 ref 都必须携带的保留策略字段（契约要求，缺失即无法回答"谁能活多久"）。
        "retention_class", "requested_expires_at", "effective_expires_at",
        "retention_policy_source", "retention_clamp_reason", "retention_clamped",
    }
    # 已完成任务 → 审计归档；请求值与生效值都写清楚（未被夹取时原因为空串）。
    assert meta["retention_class"] == "AUDIT_ARCHIVE"
    assert meta["retention_policy_source"] == "LOG_ARCHIVE_RETENTION_COMPLETED_JOB_SECONDS"
    assert meta["requested_expires_at"] and meta["effective_expires_at"]
    assert meta["expires_at"] == meta["effective_expires_at"]
    assert meta["retention_clamped"] is False and meta["retention_clamp_reason"] == ""


# ── 保留策略来自 settings ──────────────────────────────────


def test_retention_policy_is_settings_driven():
    assert process_log_archive.retention_setting_name(
        process_log_archive.ARCHIVE_CATEGORY_ACTIVE_TASK
    ) == "LOG_ARCHIVE_RETENTION_ACTIVE_TASK_SECONDS"
    assert process_log_archive.retention_setting_name(
        process_log_archive.ARCHIVE_CATEGORY_COMPLETED_JOB
    ) == "LOG_ARCHIVE_RETENTION_COMPLETED_JOB_SECONDS"
    assert process_log_archive.retention_setting_name(
        process_log_archive.ARCHIVE_CATEGORY_USER_ARTIFACT
    ) == "LOG_ARCHIVE_RETENTION_USER_ARTIFACT_SECONDS"


def test_retention_values_come_from_settings_not_literals(monkeypatch):
    monkeypatch.setattr(settings, "LOG_ARCHIVE_RETENTION_ACTIVE_TASK_SECONDS", 1234)
    monkeypatch.setattr(settings, "LOG_ARCHIVE_RETENTION_COMPLETED_JOB_SECONDS", 999_999_999)
    monkeypatch.setattr(settings, "LOG_ARCHIVE_RETENTION_USER_ARTIFACT_SECONDS", 99)

    active = process_log_archive.retention_policy(process_log_archive.ARCHIVE_CATEGORY_ACTIVE_TASK)
    completed = process_log_archive.retention_policy(process_log_archive.ARCHIVE_CATEGORY_COMPLETED_JOB)
    saved = process_log_archive.retention_policy(process_log_archive.ARCHIVE_CATEGORY_USER_ARTIFACT)
    assert active["retention_seconds"] == 1234
    assert completed["retention_seconds"] == 999_999_999
    assert saved["retention_seconds"] == 99
    assert active["retention_setting"] == "LOG_ARCHIVE_RETENTION_ACTIVE_TASK_SECONDS"
    # 冻结读取接口自身的产物 TTL 是上限，超出部分必须如实标记 clamped。
    assert active["effective_seconds"] == 1234 and active["clamped"] is False
    assert completed["effective_seconds"] == int(artifacts.ARTIFACT_TTL_SECONDS)
    assert completed["clamped"] is True


def test_terminal_jobs_use_the_completed_job_retention(monkeypatch, tmp_path):
    _enable_archive(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "LOG_ARCHIVE_RETENTION_ACTIVE_TASK_SECONDS", 111)
    monkeypatch.setattr(settings, "LOG_ARCHIVE_RETENTION_COMPLETED_JOB_SECONDS", 222)

    active_job = _job_with_steps(PROCESS_LOG_MAX_ENTRIES + 2, status=JobStatus.RUNNING, job_id="job-active")
    finished_job = _job_with_steps(PROCESS_LOG_MAX_ENTRIES + 2, status=JobStatus.COMPLETED, job_id="job-done")
    active_meta = process_log_archive.archive_process_log_overflow(active_job)
    finished_meta = process_log_archive.archive_process_log_overflow(finished_job)
    assert active_meta is not None and finished_meta is not None
    assert active_meta["category"] == process_log_archive.ARCHIVE_CATEGORY_ACTIVE_TASK
    assert active_meta["retention_seconds"] == 111
    assert finished_meta["category"] == process_log_archive.ARCHIVE_CATEGORY_COMPLETED_JOB
    assert finished_meta["retention_seconds"] == 222


def test_missing_or_broken_retention_setting_falls_back_to_short_window(monkeypatch):
    monkeypatch.setattr(settings, "LOG_ARCHIVE_RETENTION_ACTIVE_TASK_SECONDS", "not-a-number")
    assert process_log_archive.retention_seconds(
        process_log_archive.ARCHIVE_CATEGORY_ACTIVE_TASK
    ) == 1
    monkeypatch.setattr(settings, "LOG_ARCHIVE_RETENTION_ACTIVE_TASK_SECONDS", -5)
    assert process_log_archive.retention_seconds(
        process_log_archive.ARCHIVE_CATEGORY_ACTIVE_TASK
    ) == 1


def test_archive_write_failure_never_breaks_job_state(monkeypatch, tmp_path):
    _enable_archive(monkeypatch, tmp_path)
    job = _job_with_steps(PROCESS_LOG_MAX_ENTRIES + 3)

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(process_log_archive, "_write_archive_lines", _boom)
    assert process_log_archive.archive_process_log_overflow(job) is None
    assert process_log_archive.ROUTING_ARCHIVE_KEY not in job.routing
