"""产物保留类别 → 设置的唯一映射，以及"按类清理"。

背景（P0/P1）：阶段 3 把过程日志归档（``process_log_archive.jsonl``）写进了共享的
``office_outputs`` 目录，于是归档与用户产物共用同一条"整目录超 7 天删除"的兜底规则
（``GENERATED_FILES_TTL_DAYS``）：ref 上写着 30 天，文件 7 天就被删了——**静默夹取**。

本模块把生命周期拆成三种类别（定义在契约 ``lumi_contracts.execution.artifacts``）：

* ``EPHEMERAL_ARCHIVE``（运行中任务的过程日志归档）：短期，由**归档清理器**删除；
* ``AUDIT_ARCHIVE``（已完成任务的过程日志归档）：中期，由**归档清理器**删除；
* ``USER_ARTIFACT``（任务交付物 / 用户另存）：按**工作区存储策略**，由
  :func:`cleanup_user_artifacts` 删除；**归档清理器绝不删用户产物**。

"绝不静默夹取"：任何 ``requested > effective`` 都会写成 ``retention_clamp_reason``，
并同时出现在 ①产物 ref 元数据 ②产物 API 元数据 ③归档写入日志 三处。

清理依据：每个产物容器目录下的清单 ``.retention_manifest.json``（写入侧
:func:`record_retention` 维护）记录了该文件当时的类别与生效到期时间；清单缺失/损坏时
按文件名兜底分类（归档文件名 → ``AUDIT_ARCHIVE``，其余 → ``USER_ARTIFACT``），
并按"文件 mtime + 该类别策略"判定——**未知一律当用户产物，归档清理器不碰**。
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from loguru import logger

from lumi_contracts import (
    ARCHIVE_RETENTION_CLASSES,
    DEFAULT_RETENTION_CLASS,
    RetentionClass,
    RetentionDecision,
    parse_iso_utc,
    resolve_retention,
    retention_class_of,
)

#: 与契约同名的短别名（本模块内部/调用方读起来更顺）。
EPHEMERAL_ARCHIVE = RetentionClass.EPHEMERAL_ARCHIVE.value
USER_ARTIFACT = RetentionClass.USER_ARTIFACT.value
AUDIT_ARCHIVE = RetentionClass.AUDIT_ARCHIVE.value

#: 类别 → 请求保留期的来源：``(settings 字段名, 每单位的秒数)``。
#: 这是"保留期必须来自配置"的唯一映射处；用户产物的策略就是工作区存储策略本身
#: （``GENERATED_FILES_TTL_DAYS``，以天为单位），所以它不会被无谓地标记为夹取。
REQUEST_POLICY_BY_CLASS: dict[str, tuple[str, int]] = {
    EPHEMERAL_ARCHIVE: ("LOG_ARCHIVE_RETENTION_ACTIVE_TASK_SECONDS", 1),
    AUDIT_ARCHIVE: ("LOG_ARCHIVE_RETENTION_COMPLETED_JOB_SECONDS", 1),
    USER_ARTIFACT: ("GENERATED_FILES_TTL_DAYS", 86400),
}

#: 类别 → 请求保留期的 settings 字段名（便于按名字排障/断言文档一致性）。
RETENTION_SETTING_BY_CLASS: dict[str, str] = {
    cls: name for cls, (name, _) in REQUEST_POLICY_BY_CLASS.items()
}

#: 类别 → 天花板来源：归档受"冻结读取接口的产物 TTL"约束，用户产物受工作区存储策略约束。
CEILING_SOURCE_BY_CLASS: dict[str, str] = {
    EPHEMERAL_ARCHIVE: "ARTIFACT_TTL_SECONDS",
    AUDIT_ARCHIVE: "ARTIFACT_TTL_SECONDS",
    USER_ARTIFACT: "GENERATED_FILES_TTL_DAYS",
}

#: 容器目录内的保留清单（元数据文件，不算产物：不列出、不下载、不按产物清理）。
MANIFEST_FILENAME = ".retention_manifest.json"

#: 兜底保留期（秒）：配置缺失/非法时的 fail-safe（宁可短，也不变成永久保留）。
FAILSAFE_RETENTION_SECONDS = 1


def _settings() -> Any:
    from app.core.config import settings

    return settings


def _setting_seconds(name: str, *, default: int = FAILSAFE_RETENTION_SECONDS) -> int:
    """按 settings 字段名取秒数；缺失/非法退回 fail-safe（不抛错、不变永久）。"""
    try:
        raw = int(getattr(_settings(), name))
    except Exception:  # noqa: BLE001 - 配置缺失/类型不对不能变成"永久保留"
        return default
    return max(1, raw)


def retention_setting_name(retention_class: str) -> str:
    cls = retention_class_of(retention_class, default=DEFAULT_RETENTION_CLASS)
    return REQUEST_POLICY_BY_CLASS.get(cls, REQUEST_POLICY_BY_CLASS[USER_ARTIFACT])[0]


def requested_retention_seconds(retention_class: str) -> int:
    """该类别的**请求**保留期（秒）：来自 settings（按来源的单位换算）。"""
    cls = retention_class_of(retention_class, default=DEFAULT_RETENTION_CLASS)
    name, unit_seconds = REQUEST_POLICY_BY_CLASS.get(cls, REQUEST_POLICY_BY_CLASS[USER_ARTIFACT])
    return _setting_seconds(name, default=7) * unit_seconds


def ceiling_for_class(retention_class: str) -> tuple[int, str]:
    """该类别的硬上限 ``(秒, 来源名)``。

    * 归档类别：冻结读取接口的产物 TTL（``artifacts.ARTIFACT_TTL_SECONDS``）；
    * 用户产物：工作区存储策略（``GENERATED_FILES_TTL_DAYS``，同时是用户产物清理口径）。
    """
    cls = retention_class_of(retention_class, default=DEFAULT_RETENTION_CLASS)
    from app.services import artifacts as artifacts_service

    read_ceiling = max(1, int(artifacts_service.ARTIFACT_TTL_SECONDS))
    if cls in ARCHIVE_RETENTION_CLASSES:
        return read_ceiling, CEILING_SOURCE_BY_CLASS[cls]
    workspace = _setting_seconds("GENERATED_FILES_TTL_DAYS", default=7) * 86400
    return min(workspace, read_ceiling), CEILING_SOURCE_BY_CLASS[USER_ARTIFACT]


def decision_for(
    retention_class: str,
    *,
    issued_at: float | None = None,
    requested_seconds: int | None = None,
    policy_source: str = "",
) -> RetentionDecision:
    """构造保留决策（请求值 → 生效值 + 夹取原因）。"""
    cls = retention_class_of(retention_class, default=DEFAULT_RETENTION_CLASS)
    requested = (
        requested_retention_seconds(cls) if requested_seconds is None else max(1, int(requested_seconds))
    )
    source = str(policy_source or retention_setting_name(cls))
    ceiling, ceiling_source = ceiling_for_class(cls)
    return resolve_retention(
        retention_class=cls,
        requested_seconds=requested,
        requested_at=time.time() if issued_at is None else float(issued_at),
        policy_source=source,
        ceiling_seconds=ceiling,
        ceiling_source=ceiling_source,
    )


# ── 文件名兜底分类 ─────────────────────────────────────────


def archive_filenames() -> frozenset[str]:
    """归档产物文件名集合（延迟导入，避免 services 内部循环依赖）。"""
    from app.services.process_log_archive import ARCHIVE_FILENAME

    return frozenset({ARCHIVE_FILENAME})


def classify_filename(name: str) -> str:
    """按文件名兜底分类：归档文件名 → ``AUDIT_ARCHIVE``，其余 → ``USER_ARTIFACT``。

    归档文件名在写入时用哪一类由 ref 元数据/清单记录；这里只是"没有记录"时的保守
    兜底（审计归档 7 天），绝不把未知文件当归档删掉。
    """
    if Path(str(name or "")).name in archive_filenames():
        return AUDIT_ARCHIVE
    return DEFAULT_RETENTION_CLASS.value


def file_class(path: Path, entry: dict[str, Any] | None = None) -> str:
    """磁盘文件的类别：优先清单记录，其次文件名兜底。"""
    if isinstance(entry, dict):
        recorded = retention_class_of(entry.get("retention_class"), default="")
        if recorded:
            return recorded
    return classify_filename(path.name)


# ── 保留清单（清理依据）────────────────────────────────────


def manifest_path(container_dir: Path) -> Path:
    return Path(container_dir) / MANIFEST_FILENAME


def read_manifest(container_dir: Path) -> dict[str, dict[str, Any]]:
    """读取容器目录的保留清单；缺失/损坏返回空字典（不抛错）。"""
    path = manifest_path(container_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, dict)}


def record_retention(container_dir: Path, filename: str, decision: RetentionDecision) -> None:
    """把一次保留决策写进容器清单（合并写入；写失败只告警，不影响产物落盘）。"""
    target = Path(container_dir) / Path(str(filename)).name
    entries = read_manifest(container_dir)
    metadata = decision.as_metadata()
    entries[target.name] = {
        **metadata,
        "requested_seconds": decision.requested_seconds,
        "effective_seconds": decision.effective_seconds,
        "written_at": time.time(),
    }
    path = manifest_path(container_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(entries, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:  # noqa: BLE001 - 清理清单是辅助信息，不能影响产物写入
        logger.warning("[artifact-retention] 保留清单写入失败 dir={} err={}", str(container_dir)[-60:], str(exc)[:120])
        return
    if decision.clamped:
        # 夹取必须留痕：ref/API 之外，写盘时再记一条（排障时先看这里）。
        logger.warning(
            "[artifact-retention] 保留期被夹取 file={} class={} {}",
            target.name,
            decision.retention_class,
            decision.retention_clamp_reason,
        )


def entry_expires_at(path: Path, entry: dict[str, Any] | None, *, class_seconds: int) -> float:
    """文件的到期 epoch：优先清单里的生效到期时间，否则 mtime + 该类别策略。"""
    if isinstance(entry, dict):
        recorded = parse_iso_utc(entry.get("effective_expires_at"))
        if recorded > 0:
            return recorded
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return float("inf")  # 读不到状态就不删（fail-safe）
    return mtime + max(1, int(class_seconds))


# ── 按类清理 ───────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CleanupReport:
    """一次清理的结果（按类别分别计数，便于断言"没删错类"）。"""

    removed_files: int = 0
    removed_dirs: int = 0
    by_class: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"removed_files": self.removed_files, "removed_dirs": self.removed_dirs, "by_class": dict(self.by_class)}


def cleanup_outputs_by_class(
    base_dir: Path | str,
    *,
    classes: Iterable[str],
    user_ttl_days: int | None = None,
    now: float | None = None,
) -> CleanupReport:
    """只删除 ``classes`` 里类别的过期文件；其它类别原样保留。

    这是"按类清理"的唯一实现：用户产物清理器传 ``classes=(USER_ARTIFACT,)``，
    归档清理器传 ``classes=ARCHIVE_RETENTION_CLASSES``——两条路径互不越界。
    """
    wanted = {retention_class_of(item, default=DEFAULT_RETENTION_CLASS) for item in classes}
    if not wanted:
        return CleanupReport()
    base = Path(base_dir)
    if not base.exists():
        return CleanupReport()
    now_ts = time.time() if now is None else float(now)
    ttl_days = int(user_ttl_days if user_ttl_days is not None else _setting_seconds("GENERATED_FILES_TTL_DAYS", default=7))
    user_ttl_seconds = max(1, ttl_days) * 86400
    by_class: dict[str, int] = {}
    removed_files = 0
    removed_dirs = 0
    for user_dir in sorted(base.iterdir()):
        if not user_dir.is_dir():
            continue
        for container in sorted(user_dir.iterdir()):
            if not container.is_dir():
                continue
            entries = read_manifest(container)
            for path in sorted(container.iterdir()):
                if not path.is_file() or path.name == MANIFEST_FILENAME:
                    continue
                entry = entries.get(path.name)
                cls = file_class(path, entry)
                if cls not in wanted:
                    continue
                class_seconds = (
                    user_ttl_seconds if cls == USER_ARTIFACT else requested_retention_seconds(cls)
                )
                if entry_expires_at(path, entry, class_seconds=class_seconds) > now_ts:
                    continue
                try:
                    path.unlink()
                except OSError:
                    continue
                removed_files += 1
                by_class[cls] = by_class.get(cls, 0) + 1
            if not _has_payload_files(container):
                shutil.rmtree(container, ignore_errors=True)
                removed_dirs += 1
        try:
            if not any(user_dir.iterdir()):
                user_dir.rmdir()
        except OSError:
            continue
    return CleanupReport(removed_files=removed_files, removed_dirs=removed_dirs, by_class=by_class)


def _has_payload_files(container: Path) -> bool:
    """容器里是否还有非清单文件（清单本身不算产物）。"""
    try:
        return any(item.is_file() and item.name != MANIFEST_FILENAME for item in container.iterdir())
    except OSError:
        return True


def _outputs_base() -> Path:
    return Path(_settings().UPLOAD_DIR) / "office_outputs"


def cleanup_user_artifacts(ttl_days: int | None = None, *, now: float | None = None) -> CleanupReport:
    """工作区存储策略清理：**只**删 ``USER_ARTIFACT``（绝不碰归档）。"""
    return cleanup_outputs_by_class(
        _outputs_base(), classes=(USER_ARTIFACT,), user_ttl_days=ttl_days, now=now
    )


def cleanup_archive_outputs(*, now: float | None = None) -> CleanupReport:
    """归档清理器：**只**删 ``EPHEMERAL_ARCHIVE`` / ``AUDIT_ARCHIVE``（绝不碰用户产物）。"""
    return cleanup_outputs_by_class(_outputs_base(), classes=ARCHIVE_RETENTION_CLASSES, now=now)


__all__ = [
    "ARCHIVE_RETENTION_CLASSES",
    "AUDIT_ARCHIVE",
    "CEILING_SOURCE_BY_CLASS",
    "CleanupReport",
    "EPHEMERAL_ARCHIVE",
    "FAILSAFE_RETENTION_SECONDS",
    "MANIFEST_FILENAME",
    "RETENTION_SETTING_BY_CLASS",
    "USER_ARTIFACT",
    "archive_filenames",
    "ceiling_for_class",
    "classify_filename",
    "cleanup_archive_outputs",
    "cleanup_outputs_by_class",
    "cleanup_user_artifacts",
    "decision_for",
    "entry_expires_at",
    "file_class",
    "manifest_path",
    "read_manifest",
    "record_retention",
    "requested_retention_seconds",
    "retention_setting_name",
]
