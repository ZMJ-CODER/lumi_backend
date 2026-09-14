"""产物保留类别（retention classes）与**按类清理**回归。

覆盖（与 P0/P1 的验收项一一对应）：

* 三个类别 ``EPHEMERAL_ARCHIVE`` / ``USER_ARTIFACT`` / ``AUDIT_ARCHIVE`` 由契约定义，
  类别 → 设置 → 清理器的映射唯一（``artifact_retention``）；
* **按类清理不删错类**：用户产物清理器只删 ``USER_ARTIFACT``，归档清理器只删两个归档
  类别；用户产物在归档清理中必然存活；
* 不再按"整目录 mtime"连带删除（此前归档搭 office_outputs 兜底规则的便车）；
* **夹取必须被报告**：requested vs effective + 原因（设置名 + 天花板上限）；
* 产物 API（元数据 / 内容 / download-url）下发保留策略与"将于 N 天后过期"提示。
"""

from __future__ import annotations

import asyncio
import json
import os
import time

from app.agents.orchestration.models import Job, JobStatus
from app.api.v1 import artifacts as artifacts_api
from app.core.config import settings
from app.services import artifact_retention
from app.services import artifacts
from app.office import docs as office_docs
from app.services import process_log_archive
from lumi_contracts.persistence.run_view import PROCESS_LOG_MAX_ENTRIES

DAY = 86400.0


def _container(tmp_path, user: str = "u1", conv: str = "job-1"):
    return tmp_path / "office_outputs" / user / conv


def _write(path, body: str = "x", *, mtime: float | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _touch_dir(path, mtime: float) -> None:
    os.utime(path, (mtime, mtime))


# ── 类别定义与映射唯一性 ───────────────────────────────────


def test_retention_classes_are_defined_by_the_contract():
    from lumi_contracts import ARTIFACT_RETENTION_FIELDS, RETENTION_CLASSES, ArtifactRef, RetentionClass

    assert RETENTION_CLASSES == {"EPHEMERAL_ARCHIVE", "USER_ARTIFACT", "AUDIT_ARCHIVE"}
    assert {item.value for item in RetentionClass} == RETENTION_CLASSES
    # 每一条产物 ref 都必须能携带这五个字段（契约级要求）。
    assert set(ARTIFACT_RETENTION_FIELDS) == {
        "retention_class",
        "requested_expires_at",
        "effective_expires_at",
        "retention_policy_source",
        "retention_clamp_reason",
    }
    assert set(ARTIFACT_RETENTION_FIELDS) <= set(ArtifactRef.model_fields)


def test_class_to_setting_mapping_is_unique_and_settings_driven():
    assert artifact_retention.retention_setting_name(artifact_retention.EPHEMERAL_ARCHIVE) == (
        "LOG_ARCHIVE_RETENTION_ACTIVE_TASK_SECONDS"
    )
    assert artifact_retention.retention_setting_name(artifact_retention.AUDIT_ARCHIVE) == (
        "LOG_ARCHIVE_RETENTION_COMPLETED_JOB_SECONDS"
    )
    assert artifact_retention.retention_setting_name(artifact_retention.USER_ARTIFACT) == "GENERATED_FILES_TTL_DAYS"


def test_archive_categories_map_to_retention_classes():
    assert process_log_archive.retention_class(process_log_archive.ARCHIVE_CATEGORY_ACTIVE_TASK) == (
        artifact_retention.EPHEMERAL_ARCHIVE
    )
    assert process_log_archive.retention_class(process_log_archive.ARCHIVE_CATEGORY_COMPLETED_JOB) == (
        artifact_retention.AUDIT_ARCHIVE
    )
    assert process_log_archive.retention_class(process_log_archive.ARCHIVE_CATEGORY_USER_ARTIFACT) == (
        artifact_retention.USER_ARTIFACT
    )


# ── 夹取必须被报告（requested vs effective + 原因）─────────


def test_clamp_is_reported_with_requested_effective_and_reason(monkeypatch):
    monkeypatch.setattr(settings, "LOG_ARCHIVE_RETENTION_COMPLETED_JOB_SECONDS", 40 * 86400)

    decision = artifact_retention.decision_for(artifact_retention.AUDIT_ARCHIVE)

    assert decision.requested_seconds == 40 * 86400
    assert decision.effective_seconds == int(artifacts.ARTIFACT_TTL_SECONDS)
    assert decision.clamped is True
    assert decision.requested_expires_at != decision.effective_expires_at
    # 原因必须能定位"谁请求的、被谁夹的、夹到多少"。
    assert "LOG_ARCHIVE_RETENTION_COMPLETED_JOB_SECONDS" in decision.retention_clamp_reason
    assert "ARTIFACT_TTL_SECONDS" in decision.retention_clamp_reason
    assert str(decision.requested_seconds) in decision.retention_clamp_reason
    metadata = decision.as_metadata()
    assert metadata["retention_clamped"] is True and metadata["retention_clamp_reason"]


def test_unclamped_decision_has_no_reason(monkeypatch):
    monkeypatch.setattr(settings, "LOG_ARCHIVE_RETENTION_ACTIVE_TASK_SECONDS", 3600)
    decision = artifact_retention.decision_for(artifact_retention.EPHEMERAL_ARCHIVE)
    assert (decision.requested_seconds, decision.effective_seconds) == (3600, 3600)
    assert decision.clamped is False and decision.retention_clamp_reason == ""


def test_user_artifact_policy_is_the_workspace_setting(monkeypatch):
    monkeypatch.setattr(settings, "GENERATED_FILES_TTL_DAYS", 3)
    decision = artifact_retention.decision_for(artifact_retention.USER_ARTIFACT)
    assert decision.requested_seconds == 3 * 86400
    assert decision.effective_seconds == 3 * 86400  # 请求值就是工作区策略，不该被误标夹取
    assert decision.retention_policy_source == "GENERATED_FILES_TTL_DAYS"
    assert decision.clamped is False


# ── 按类清理：绝不删错类 ───────────────────────────────────


def test_user_artifact_cleanup_never_deletes_archives(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    now = time.time()
    container = _container(tmp_path)

    stale_user = _write(container / "old-report.docx", mtime=now - 10 * DAY)
    fresh_user = _write(container / "new-report.docx", mtime=now)
    ephemeral = _write(container / process_log_archive.ARCHIVE_FILENAME, mtime=now - 30 * DAY)
    artifact_retention.record_retention(
        container,
        process_log_archive.ARCHIVE_FILENAME,
        artifact_retention.decision_for(
            artifact_retention.EPHEMERAL_ARCHIVE, issued_at=now - 2 * DAY, requested_seconds=DAY
        ),
    )

    report = artifact_retention.cleanup_user_artifacts(ttl_days=7, now=now)

    assert report.removed_files == 1 and report.by_class == {artifact_retention.USER_ARTIFACT: 1}
    assert not stale_user.exists()
    assert fresh_user.exists()
    assert ephemeral.exists(), "用户产物清理器绝不能删归档"


def test_archive_cleanup_never_deletes_user_artifacts(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    now = time.time()
    container = _container(tmp_path)

    # 目录 mtime 很旧 + 用户产物很旧：旧实现会整目录 rmtree，这里必须只按文件类别判定。
    user_file = _write(container / "deliverable.docx", mtime=now - 40 * DAY)
    _write(container / "fresh-user.docx", mtime=now)
    _touch_dir(container, now - 40 * DAY)

    audit = _write(container / process_log_archive.ARCHIVE_FILENAME, mtime=now - 40 * DAY)
    artifact_retention.record_retention(
        container,
        process_log_archive.ARCHIVE_FILENAME,
        artifact_retention.decision_for(
            artifact_retention.AUDIT_ARCHIVE, issued_at=now - 9 * DAY, requested_seconds=7 * DAY
        ),
    )

    other = _container(tmp_path, conv="job-2")
    live_audit = _write(other / process_log_archive.ARCHIVE_FILENAME, mtime=now)
    artifact_retention.record_retention(
        other,
        process_log_archive.ARCHIVE_FILENAME,
        artifact_retention.decision_for(artifact_retention.AUDIT_ARCHIVE, issued_at=now),
    )

    report = artifact_retention.cleanup_archive_outputs(now=now)

    assert report.by_class == {artifact_retention.AUDIT_ARCHIVE: 1}, report.as_dict()
    assert not audit.exists()
    assert live_audit.exists(), "未到期的审计归档不得被删"
    assert user_file.exists() and user_file.parent.exists(), "归档清理器绝不能删用户产物"
    assert _container(tmp_path).exists()


def test_ephemeral_and_audit_are_cleaned_by_their_own_class_ttl(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    now = time.time()
    ephemeral_dir = _container(tmp_path, conv="job-ephemeral")
    audit_dir = _container(tmp_path, conv="job-audit")

    ephemeral = _write(ephemeral_dir / process_log_archive.ARCHIVE_FILENAME, mtime=now - 2 * DAY)
    artifact_retention.record_retention(
        ephemeral_dir,
        process_log_archive.ARCHIVE_FILENAME,
        artifact_retention.decision_for(
            artifact_retention.EPHEMERAL_ARCHIVE, issued_at=now - 2 * DAY, requested_seconds=DAY
        ),
    )
    audit = _write(audit_dir / process_log_archive.ARCHIVE_FILENAME, mtime=now - 2 * DAY)
    artifact_retention.record_retention(
        audit_dir,
        process_log_archive.ARCHIVE_FILENAME,
        artifact_retention.decision_for(artifact_retention.AUDIT_ARCHIVE, issued_at=now - 2 * DAY),
    )

    report = artifact_retention.cleanup_archive_outputs(now=now)

    # 同为"2 天前的归档文件"：临时归档已到期（24h）被删，审计归档（7d）保留。
    assert report.by_class == {artifact_retention.EPHEMERAL_ARCHIVE: 1}
    assert not ephemeral.exists()
    assert audit.exists()


def test_archive_cleanup_falls_back_to_filename_class_without_manifest(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    now = time.time()
    container = _container(tmp_path)
    orphan = _write(container / process_log_archive.ARCHIVE_FILENAME, mtime=now - 8 * DAY)
    unknown = _write(container / "mystery.bin", mtime=now - 100 * DAY)

    report = artifact_retention.cleanup_archive_outputs(now=now)

    assert report.by_class == {artifact_retention.AUDIT_ARCHIVE: 1}
    assert not orphan.exists()
    assert unknown.exists(), "没有类别信息的文件一律当用户产物，归档清理器不碰"


def test_office_docs_cleaner_only_removes_user_artifacts(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    now = time.time()
    container = _container(tmp_path)
    user_file = _write(container / "old.txt", mtime=now - 30 * DAY)
    archive = _write(container / process_log_archive.ARCHIVE_FILENAME, mtime=now - 30 * DAY)

    removed = office_docs.cleanup_generic_outputs(ttl_days=7)

    assert removed == 1
    assert not user_file.exists()
    assert archive.exists()


def test_retention_manifest_is_not_listed_as_an_output(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    container = _container(tmp_path)
    _write(container / "report.md")
    artifact_retention.record_retention(
        container, "report.md", artifact_retention.decision_for(artifact_retention.USER_ARTIFACT)
    )

    listed = [item["name"] for item in office_docs.list_generic_outputs("u1", "job-1")]

    assert listed == ["report.md"]


# ── 归档写入把类别/清单落盘（清理器据此判定）───────────────


def _job_with_steps(count: int, *, status: JobStatus, job_id: str) -> Job:
    steps = [{"id": f"s{i}", "title": f"步骤 {i}", "status": "completed"} for i in range(count)]
    return Job(
        job_id=job_id,
        user_id="u1",
        user_role="user",
        request="归档保留类别测试",
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


def test_archive_write_records_class_in_manifest_per_job_status(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ARCHIVE_CONTENT_V2", True)
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    running = _job_with_steps(PROCESS_LOG_MAX_ENTRIES + 5, status=JobStatus.RUNNING, job_id="job-run")
    done = _job_with_steps(PROCESS_LOG_MAX_ENTRIES + 5, status=JobStatus.COMPLETED, job_id="job-done")

    running_meta = process_log_archive.archive_process_log_overflow(running)
    done_meta = process_log_archive.archive_process_log_overflow(done)

    assert running_meta is not None and done_meta is not None
    assert running_meta["retention_class"] == artifact_retention.EPHEMERAL_ARCHIVE
    assert done_meta["retention_class"] == artifact_retention.AUDIT_ARCHIVE
    entries = artifact_retention.read_manifest(process_log_archive.archive_path(done).parent)
    recorded = entries[process_log_archive.ARCHIVE_FILENAME]
    assert recorded["retention_class"] == artifact_retention.AUDIT_ARCHIVE
    assert recorded["effective_expires_at"] == done_meta["effective_expires_at"]


def test_clamped_archive_ref_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ARCHIVE_CONTENT_V2", True)
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "LOG_ARCHIVE_RETENTION_COMPLETED_JOB_SECONDS", 40 * 86400)
    job = _job_with_steps(PROCESS_LOG_MAX_ENTRIES + 3, status=JobStatus.COMPLETED, job_id="job-clamp")

    meta = process_log_archive.archive_process_log_overflow(job)

    assert meta is not None
    assert meta["requested_expires_at"] != meta["effective_expires_at"]
    assert meta["clamped"] is True and meta["retention_clamped"] is True
    assert "LOG_ARCHIVE_RETENTION_COMPLETED_JOB_SECONDS" in meta["retention_clamp_reason"]
    policy = process_log_archive.retention_policy(process_log_archive.ARCHIVE_CATEGORY_COMPLETED_JOB)
    assert policy["effective_seconds"] == int(artifacts.ARTIFACT_TTL_SECONDS) < policy["retention_seconds"]


# ── 前端可见：API 下发保留策略与到期提示 ───────────────────


def test_artifact_metadata_exposes_retention_policy(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ARCHIVE_CONTENT_V2", True)
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "LOG_ARCHIVE_RETENTION_COMPLETED_JOB_SECONDS", 40 * 86400)
    job = _job_with_steps(PROCESS_LOG_MAX_ENTRIES + 3, status=JobStatus.COMPLETED, job_id="job-api")
    meta = process_log_archive.archive_process_log_overflow(job)
    assert meta is not None

    body = asyncio.run(artifacts_api.get_artifact(meta["ref"], {"sub": "u1"}))
    data = body["data"]

    assert data["retention_class"] == artifact_retention.AUDIT_ARCHIVE
    assert data["requested_expires_at"] and data["effective_expires_at"]
    assert data["retention_policy_source"] == "LOG_ARCHIVE_RETENTION_COMPLETED_JOB_SECONDS"
    assert data["retention_clamp_reason"]
    assert data["retention_clamped"] is True
    assert 0 <= data["days_until_expiry"] <= artifacts.ARTIFACT_TTL_DAYS
    assert data["retention_notice"].startswith("该产物受当前存储策略限制，将于")
    assert str(tmp_path) not in json.dumps(data, ensure_ascii=False)


def test_artifact_content_response_exposes_retention_policy(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ARCHIVE_CONTENT_V2", True)
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    job = _job_with_steps(PROCESS_LOG_MAX_ENTRIES + 3, status=JobStatus.COMPLETED, job_id="job-content")
    meta = process_log_archive.archive_process_log_overflow(job)
    assert meta is not None

    body = asyncio.run(
        artifacts_api.get_artifact_content(
            meta["ref"], job_id="", workspace_id="", max_bytes=65_536, payload={"sub": "u1"}
        )
    )
    data = body["data"]
    assert data["retention_class"] == artifact_retention.AUDIT_ARCHIVE
    assert data["effective_expires_at"] == meta["effective_expires_at"]
    assert data["retention_clamp_reason"] == ""
    assert "天后过期" in data["retention_notice"]


def test_user_artifact_refs_carry_retention_fields_without_clamping():
    ref = artifacts.artifact_from_output("job-1", {"name": "report.md", "size": 12})
    assert ref is not None
    assert ref["retention_class"] == artifact_retention.USER_ARTIFACT
    assert ref["retention_policy_source"] == "GENERATED_FILES_TTL_DAYS"
    assert ref["requested_expires_at"] == ref["effective_expires_at"]
    assert ref["retention_clamped"] is False and ref["retention_clamp_reason"] == ""
    # 旧引用（未签类别）按文件名兜底分类，仍能回答保留策略。
    legacy = artifacts.make_artifact_id("job-1", "report.md")
    record = artifacts.retention_decision_for(artifacts.parse_artifact_id(legacy))
    assert record.retention_class == artifact_retention.USER_ARTIFACT
    assert record.effective_expires_at


def test_artifact_created_event_carries_retention_fields():
    """SSE 的 ``artifact_created`` 载荷也要带保留策略（前端就地提示，无需再取元数据）。"""
    from lumi_contracts.events.envelope import build_payload

    ref = artifacts.artifact_from_output("job-1", {"name": "report.md", "size": 12})
    assert ref is not None
    payload = build_payload("artifact_created", ref)

    assert payload["retention_class"] == artifact_retention.USER_ARTIFACT
    assert payload["effective_expires_at"] == ref["effective_expires_at"]
    assert payload["retention_policy_source"] == "GENERATED_FILES_TTL_DAYS"
    assert payload["retention_clamp_reason"] == ""
    assert payload["retention_notice"].endswith("天后过期")
    # 白名单仍然有效：危险键不会因为新增字段而被放行。
    assert "internal_locator" not in payload and "download_url" not in payload


def test_download_url_reports_artifact_expiry_separately_from_token(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    container = _container(tmp_path, conv="job-dl")
    container.mkdir(parents=True, exist_ok=True)
    (container / "report.md").write_text("hi", encoding="utf-8")
    artifact_id = artifacts.make_artifact_id(
        "job-dl", "report.md", retention_class=artifact_retention.USER_ARTIFACT
    )

    body = asyncio.run(artifacts_api.create_artifact_download_url(artifact_id, {"sub": "u1"}))
    data = body["data"]

    assert data["expires_in"] <= artifacts.ARTIFACT_DOWNLOAD_URL_MAX_TTL_SECONDS
    assert data["artifact_expires_at"] == data["effective_expires_at"]
    assert data["retention_class"] == artifact_retention.USER_ARTIFACT
