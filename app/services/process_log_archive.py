"""阶段 3：过程日志溢出的**真实归档**（``ARCHIVE_CONTENT_V2``）。

背景：``JobRunView.roll_process_log()`` 是硬规则①的落点——快照只留最近
``PROCESS_LOG_MAX_ENTRIES`` 条，窗口外的更早条目必须由调用方转存，再用
``with_log_archive(ref, count)`` 记引用。此前没有生产调用方，``log_archive_ref``
永远是空串；本模块把它接上：

* **不新建存储**：归档文件写进既有通用产物目录
  （``app/office/docs.py::generic_outputs_dir``，按用户 × 任务隔离），
  引用用既有 ``artifacts.make_artifact_id`` 签名，读取走冻结的
  ``GET /api/v1/artifacts/{ref}/content``；
* **内容真实**：写的是窗口外条目的 JSON Lines（一行一条、UTF-8、有序、可读）；
* **幂等**：文件名固定（``process_log_archive.jsonl``），每次按当前全量日志重算
  "窗口外内容"并覆盖写入，因此重复保存不会产生重复文件、不会重复条目；
* **保留期来自 settings**：按归档类别（运行中任务 / 已完成任务 / 用户另存产物）
  读取 ``LOG_ARCHIVE_RETENTION_*_SECONDS``，代码里不写死 TTL；每个类别映射到一个
  **保留类别**（``EPHEMERAL_ARCHIVE`` / ``AUDIT_ARCHIVE`` / ``USER_ARTIFACT``，
  见 ``app/services/artifact_retention.py``），由**归档清理器**按类删除——归档不再搭
  "office_outputs 整目录 7 天"的便车，ref 上的 30 天也不会被静默夹成 7 天
  （请求值 > 生效值时会把原因写进 ref 元数据与日志）。

开关关闭时 :func:`archive_process_log_overflow` 是纯空操作：不写文件、不动 routing，
行为与改造前逐字节一致。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from loguru import logger

from app.agents.orchestration.models import Job, JobStatus
from app.services import artifact_retention
from lumi_contracts import JobRunView, RetentionClass, RetentionDecision
from lumi_contracts.persistence.run_view import PROCESS_LOG_MAX_ENTRIES

#: 归档类别（保留策略的键；也是 ref 元数据里的 ``category``）。
ARCHIVE_CATEGORY_ACTIVE_TASK = "active_task_log"
ARCHIVE_CATEGORY_COMPLETED_JOB = "completed_job_archive"
ARCHIVE_CATEGORY_USER_ARTIFACT = "user_saved_artifact"

#: 类别 → settings 字段名（"保留期必须来自配置"的唯一映射处）。
RETENTION_SETTING_BY_CATEGORY: dict[str, str] = {
    ARCHIVE_CATEGORY_ACTIVE_TASK: "LOG_ARCHIVE_RETENTION_ACTIVE_TASK_SECONDS",
    ARCHIVE_CATEGORY_COMPLETED_JOB: "LOG_ARCHIVE_RETENTION_COMPLETED_JOB_SECONDS",
    ARCHIVE_CATEGORY_USER_ARTIFACT: "LOG_ARCHIVE_RETENTION_USER_ARTIFACT_SECONDS",
}

#: 归档类别 → 保留类别（决定"谁来清"）：运行中=临时归档，已完成=审计归档，
#: 用户另存=用户产物（归工作区策略清理器，归档清理器不碰）。
RETENTION_CLASS_BY_CATEGORY: dict[str, str] = {
    ARCHIVE_CATEGORY_ACTIVE_TASK: RetentionClass.EPHEMERAL_ARCHIVE.value,
    ARCHIVE_CATEGORY_COMPLETED_JOB: RetentionClass.AUDIT_ARCHIVE.value,
    ARCHIVE_CATEGORY_USER_ARTIFACT: RetentionClass.USER_ARTIFACT.value,
}

#: 归档文件名（固定名 → 重复归档覆盖同一份，不产生垃圾产物）。
ARCHIVE_FILENAME = "process_log_archive.jsonl"

#: 任务已定局的状态（归档归入"已完成任务"保留策略）。
_TERMINAL_JOB_STATUSES = {
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
    JobStatus.INTERRUPTED,
}

#: routing 里存放归档引用的键（Job 模型无需新增字段，旧快照天然兼容）。
ROUTING_ARCHIVE_KEY = "process_log_archive"


def is_enabled() -> bool:
    """``ARCHIVE_CONTENT_V2``（默认关闭；关闭时不写文件、不加 routing 字段）。"""
    from app.platform.runtime.feature_flags import feature_enabled

    return feature_enabled("ARCHIVE_CONTENT_V2")


def archive_category(job: Job) -> str:
    """按任务状态判定归档类别（保留策略据此取值）。"""
    status = getattr(job, "status", None)
    value = getattr(status, "value", status)
    if str(value) in {item.value for item in _TERMINAL_JOB_STATUSES}:
        return ARCHIVE_CATEGORY_COMPLETED_JOB
    return ARCHIVE_CATEGORY_ACTIVE_TASK


def retention_setting_name(category: str) -> str:
    return RETENTION_SETTING_BY_CATEGORY.get(str(category or ""), RETENTION_SETTING_BY_CATEGORY[ARCHIVE_CATEGORY_ACTIVE_TASK])


def retention_class(category: str) -> str:
    """归档类别 → 保留类别（决定由哪个清理器负责）。"""
    return RETENTION_CLASS_BY_CATEGORY.get(
        str(category or ""), RETENTION_CLASS_BY_CATEGORY[ARCHIVE_CATEGORY_ACTIVE_TASK]
    )


def retention_seconds(category: str) -> int:
    """类别保留期（秒）：从 settings 读取，缺失/非法时退回 1 秒（fail-safe 短期）。"""
    from app.core.config import settings

    name = retention_setting_name(category)
    try:
        raw = int(getattr(settings, name))
    except Exception:  # noqa: BLE001 - 配置缺失/类型不对不能变成"永久保留"
        return 1
    return max(1, raw)


def retention_decision(category: str, *, issued_at: float | None = None) -> RetentionDecision:
    """类别保留决策（请求值 → 生效值 + 夹取原因，绝不静默夹取）。"""
    return artifact_retention.decision_for(
        retention_class(category),
        issued_at=issued_at,
        requested_seconds=retention_seconds(category),
        policy_source=retention_setting_name(category),
    )


def effective_retention_seconds(category: str) -> int:
    """实际生效保留期：类别策略与天花板（读取接口 TTL / 工作区策略）取较小者。"""
    return retention_decision(category).effective_seconds


def retention_policy(
    category: str,
    *,
    issued_at: float | None = None,
    decision: RetentionDecision | None = None,
) -> dict[str, Any]:
    """保留策略快照（进 ref 元数据，便于审计"这条归档能活多久、依据是什么"）。

    同时给出契约要求的 ``retention_class`` / ``requested_expires_at`` /
    ``effective_expires_at`` / ``retention_policy_source`` 与（被夹取时的）
    ``retention_clamp_reason``；``clamped`` / ``retention_seconds`` / ``effective_seconds``
    为既有字段（前端与旧测试仍按它们读）。已算好的决策可直接传入（避免重复计算）。
    """
    resolved = decision or retention_decision(category, issued_at=issued_at)
    return {
        "category": str(category or ARCHIVE_CATEGORY_ACTIVE_TASK),
        "retention_setting": retention_setting_name(category),
        "retention_seconds": resolved.requested_seconds,
        "effective_seconds": resolved.effective_seconds,
        "clamped": resolved.clamped,
        **resolved.as_metadata(),
    }


def archive_path(job: Job) -> Path:
    """归档文件的真实落盘位置（既有通用产物目录，不新增存储）。"""
    from app.office.docs import generic_outputs_dir

    return generic_outputs_dir(str(job.user_id or ""), str(job.job_id or "")) / ARCHIVE_FILENAME


def _write_archive_lines(path: Path, rows: list[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows)
    path.write_text(body, encoding="utf-8")
    return len(body.encode("utf-8"))


def archive_metadata(job: Job) -> dict[str, Any] | None:
    """读取当前任务已记录的归档引用（没有则 ``None``）。"""
    routing = getattr(job, "routing", None)
    if not isinstance(routing, dict):
        return None
    meta = routing.get(ROUTING_ARCHIVE_KEY)
    return dict(meta) if isinstance(meta, dict) and meta.get("ref") else None


def roll_process_log_view(
    job: Job, *, entries: list[dict[str, Any]] | None = None
) -> tuple[JobRunView, list[Any]]:
    """把任务的全量过程日志滚进快照窗口，交还窗口外条目（契约 API 的唯一用法）。"""
    from app.contracts.process_log import merge_job_process_log, process_log_payload

    full = entries if entries is not None else process_log_payload(merge_job_process_log(job, limit=0))
    view = JobRunView(job_id=str(job.job_id or ""))
    # roll_process_log 内部按去重键合并并切"保留窗口 / 归档"两段。
    return view.roll_process_log(full, keep=PROCESS_LOG_MAX_ENTRIES)


def archive_process_log_overflow(job: Job) -> dict[str, Any] | None:
    """窗口溢出时把更早条目写成归档产物，并把真实引用记回 routing。

    返回归档元数据（``None`` 表示开关关闭或没有溢出）。开关关闭时不产生任何副作用。
    """
    if not is_enabled():
        return None
    view, archived = roll_process_log_view(job)
    if not archived:
        return None
    rows = [
        entry.model_dump(mode="json", exclude_none=True) if hasattr(entry, "model_dump") else dict(entry)
        for entry in archived
    ]
    category = archive_category(job)
    cls = retention_class(category)
    path = archive_path(job)
    try:
        size_bytes = _write_archive_lines(path, rows)
    except OSError as exc:  # noqa: BLE001 - 归档失败不能让任务状态保存失败
        logger.warning("[process-log-archive] 归档写入失败 job={} err={}", str(job.job_id)[:12], str(exc)[:120])
        return None
    from app.services import artifacts

    issued_at = time.time()
    decision = retention_decision(category, issued_at=issued_at)
    # 把"请求保留期"签进引用：产物元数据接口只有 artifact_id，靠它回答"请求多久、被谁夹到多久"。
    ref = artifacts.make_artifact_id(
        str(job.job_id or ""),
        ARCHIVE_FILENAME,
        issued_at=issued_at,
        retention_class=cls,
        retention_seconds=decision.requested_seconds,
    )
    # 清理依据：容器清单记录该文件的类别与生效到期时间，归档清理器据此**按类**删除。
    artifact_retention.record_retention(path.parent, ARCHIVE_FILENAME, decision)
    # 契约 API：执行日志契约的 ``process_log`` 命名空间之外，用 with_log_archive 把
    # 引用与条数写进快照，保证 ``log_archive_ref`` 是真实可读的。
    view = view.with_log_archive(ref, count=int(view.log_archive_count or len(rows)))
    meta: dict[str, Any] = {
        "ref": view.log_archive_ref,
        "count": int(view.log_archive_count or len(rows)),
        "filename": ARCHIVE_FILENAME,
        "size_bytes": size_bytes,
        "written_at": issued_at,
        # ``expires_at`` 保留原名（前端既有字段），语义 = 生效到期时间。
        "expires_at": decision.effective_expires_at,
        "window_entries": len(view.process_log),
        **retention_policy(category, decision=decision),
    }
    job.routing = dict(job.routing or {})
    job.routing[ROUTING_ARCHIVE_KEY] = meta
    return meta


def archive_view_fields(job: Job) -> dict[str, Any]:
    """``run_view`` 里要补的归档字段（开关关闭或没有归档时返回空字典）。"""
    if not is_enabled():
        return {}
    meta = archive_metadata(job)
    if meta is None:
        return {}
    return {
        "log_archive_ref": str(meta.get("ref") or ""),
        "log_archive_count": int(meta.get("count") or 0),
        "log_archive_category": str(meta.get("category") or ""),
        "log_archive_expires_at": str(meta.get("expires_at") or ""),
        "truncated": False,
    }


__all__ = [
    "ARCHIVE_CATEGORY_ACTIVE_TASK",
    "ARCHIVE_CATEGORY_COMPLETED_JOB",
    "ARCHIVE_CATEGORY_USER_ARTIFACT",
    "ARCHIVE_FILENAME",
    "RETENTION_CLASS_BY_CATEGORY",
    "RETENTION_SETTING_BY_CATEGORY",
    "ROUTING_ARCHIVE_KEY",
    "archive_category",
    "archive_metadata",
    "archive_path",
    "archive_process_log_overflow",
    "archive_view_fields",
    "effective_retention_seconds",
    "is_enabled",
    "retention_class",
    "retention_decision",
    "retention_policy",
    "retention_seconds",
    "retention_setting_name",
    "roll_process_log_view",
]
