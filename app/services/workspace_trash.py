"""工作区回收站（``.lumi_trash``）：索引记录、保留期与配额。

语义（与客户端 ``CAPABILITY_BRIDGE.md`` §11.4/§11.5 对齐，**客户端是回收站的主人**）：

* 客户端 ``workspace_delete`` 默认用**同盘 rename** 把目标原子移入 ``.lumi_trash/<trash_id>``
  （不读内容 ⇒ 二进制文件同样可以进回收站），并在 ``.lumi_trash/trash.json`` 记录：
  ``trash_id / logical_path / kind / bytes / files / dirs / deleted_at / task_id /
  conversation_id / restorable / reason / expires_at``；
* 索引保留期 **7 天**、上限 **500 条**（与本文件 :class:`TrashPolicy` 默认值一致）；
* ``.lumi_trash`` **默认不出现在 list 结果**、**不能被 read/search 读取**，只能经
  恢复/清理接口访问（后端 navigator 与客户端 navigator 都硬拒）；
* 恢复用 ``workspace_move``（rename 回去，天然处理目录与二进制），清理用暂存删除 + 一次提交；
* 回收站移动失败时**不能**报告删除成功（客户端 ``TRASH_MOVE_FAILED`` 原样上报）。

本模块不做 IO：读写由调用方注入（生产走客户端原子工具，测试用内存实现），因此可以纯函数式地
测保留期/配额/记录迁移。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from app.contracts.operations.common import (
    TRASH_DIRNAME,
    TrashPolicy,
    is_trash_path,
    normalize_rel_path,
)
from app.contracts.operations.delete import TrashRecord
from app.contracts.operations.errors import OperationErrorCode

#: 索引文件名（客户端写的就是这个名字；后端只读+按需重写）。
INDEX_VERSION = 1
INDEX_NAME = "trash.json"

#: 单条恢复/清理一次最多处理的条目数（防止一次清理把工作区写爆）。
MAX_PURGE_BATCH = 100


class TrashStore(Protocol):
    """回收站 IO 抽象（生产：客户端 workspace_read/workspace_write）。"""

    async def read_text(self, path: str) -> str | None: ...

    async def write_text(self, path: str, content: str) -> bool: ...

    async def list_dir(self, path: str) -> list[dict[str, Any]]: ...


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


class TrashIndex:
    """回收站索引（``.lumi_trash/trash.json``）：记录进出的唯一台账。

    能读两种形态：客户端写的（``trash_id``）与早期后端写的（``entry_id``）；
    ``dumps()`` 一律输出**客户端形态**，保证工作区面板与后端看到同一份记录。
    """

    def __init__(
        self,
        records: list[TrashRecord] | None = None,
        *,
        retention_days: int = 7,
    ) -> None:
        self._records: dict[str, TrashRecord] = {
            item.entry_id: item for item in (records or []) if item.entry_id
        }
        self._retention_days = int(retention_days or 7)

    @property
    def retention_days(self) -> int:
        return self._retention_days

    # ── 序列化 ────────────────────────────────────────────

    @classmethod
    def loads(cls, raw: str | None) -> "TrashIndex":
        if not raw:
            return cls()
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            # 索引损坏时按"空索引"继续：宁可丢台账，也不能让损坏的 JSON 阻断删除/恢复
            # （内容仍在 .lumi_trash/ 下，可人工处理）。
            return cls()
        retention = 7
        rows: Any = None
        if isinstance(payload, dict):
            rows = payload.get("entries")
            try:
                retention = int(payload.get("retention_days") or 7)
            except (TypeError, ValueError):
                retention = 7
        elif isinstance(payload, list):
            rows = payload
        records: list[TrashRecord] = []
        for item in rows if isinstance(rows, list) else []:
            if not isinstance(item, dict):
                continue
            record = _record_from_index_row(item, retention_days=retention)
            if record is not None:
                records.append(record)
        return cls(records, retention_days=retention)

    def dumps(self) -> str:
        """输出客户端兼容的索引（工作区面板与后端共用同一份 ``trash.json``）。"""
        return json.dumps(
            {
                "version": INDEX_VERSION,
                "retention_days": int(self._retention_days),
                "updated_at": now_iso(),
                "entries": [_index_row(item) for item in self.records()],
            },
            ensure_ascii=False,
            indent=2,
        )

    # ── 增删查 ────────────────────────────────────────────

    def records(self) -> list[TrashRecord]:
        return sorted(self._records.values(), key=lambda item: item.deleted_at, reverse=True)

    def get(self, entry_id: str) -> TrashRecord | None:
        return self._records.get(str(entry_id or ""))

    def add(self, record: TrashRecord) -> TrashRecord:
        self._records[record.entry_id] = record
        return record

    def remove(self, entry_id: str) -> TrashRecord | None:
        return self._records.pop(str(entry_id or ""), None)

    def update(self, entry_id: str, **fields: Any) -> TrashRecord | None:
        current = self._records.get(str(entry_id or ""))
        if current is None:
            return None
        updated = current.model_copy(update=fields)
        self._records[updated.entry_id] = updated
        return updated

    def __len__(self) -> int:
        return len(self._records)


def _record_from_index_row(item: dict[str, Any], *, retention_days: int) -> TrashRecord | None:
    """索引行 → :class:`TrashRecord`（兼容客户端的 ``trash_id`` 与旧 ``entry_id``）。"""
    entry_id = str(item.get("trash_id") or item.get("entry_id") or "").strip()
    if not entry_id:
        return None
    logical = normalize_rel_path(item.get("logical_path") or item.get("path") or "")
    kind = str(item.get("kind") or "file")
    try:
        return TrashRecord(
            entry_id=entry_id,
            logical_path=logical,
            trash_path=str(item.get("trash_path") or f"{TRASH_DIRNAME}/{entry_id}"),
            kind=kind,
            is_dir=bool(item.get("is_dir")) or kind == "dir",
            recursive=bool(item.get("recursive")),
            bytes=int(item.get("bytes") or 0),
            revision=str(item.get("revision") or ""),
            created_at=str(item.get("deleted_at") or item.get("created_at") or ""),
            deleted_at=str(item.get("deleted_at") or ""),
            job_id=str(item.get("task_id") or item.get("job_id") or ""),
            task_id=str(item.get("task_id") or item.get("job_id") or ""),
            conversation_id=str(item.get("conversation_id") or ""),
            user_id=str(item.get("user_id") or ""),
            device_id=str(item.get("device_id") or ""),
            workspace_id=str(item.get("workspace_id") or ""),
            retention_days=int(item.get("retention_days") or retention_days),
            expires_at=str(item.get("expires_at") or ""),
            restorable=item.get("restorable") is not False,
            restored_at=str(item.get("restored_at") or ""),
            restore_revision=str(item.get("restore_revision") or ""),
        )
    except Exception:  # noqa: BLE001 - 单条损坏不影响其余记录
        return None


def _index_row(record: TrashRecord) -> dict[str, Any]:
    """:class:`TrashRecord` → 客户端兼容的索引行。"""
    return {
        "trash_id": record.entry_id,
        "logical_path": record.logical_path,
        "kind": "dir" if record.is_dir else (record.kind or "file"),
        "bytes": int(record.bytes or 0),
        "files": int(record.files or (0 if record.is_dir else 1)),
        "dirs": int(record.dirs or (1 if record.is_dir else 0)),
        "deleted_at": record.deleted_at or record.created_at,
        "task_id": record.task_id or record.job_id,
        "conversation_id": record.conversation_id,
        "restorable": bool(record.restorable),
        "reason": record.reason,
        "expires_at": record.expires_at,
        "retention_days": int(record.retention_days or 7),
        "revision": record.revision,
        "restored_at": record.restored_at,
        "restore_revision": record.restore_revision,
    }


def build_record(
    *,
    logical_path: str,
    entry_id: str,
    revision: str = "",
    bytes_size: int = 0,
    files: int = 0,
    dirs: int = 0,
    is_dir: bool = False,
    recursive: bool = False,
    policy: TrashPolicy | None = None,
    context: Any = None,
    kind: str = "file",
    reason: str = "",
    expires_at: str = "",
    deleted_at: str = "",
) -> TrashRecord:
    """构造一条回收站记录（删除时间、保留期与恢复信息都在这里固定下来）。"""
    active = policy or TrashPolicy()
    stamp = deleted_at or now_iso()
    expires = expires_at
    if not expires:
        parsed = _parse_iso(stamp) or datetime.now(timezone.utc)
        expires = (parsed + timedelta(days=int(active.retention_days))).isoformat()
    rel = normalize_rel_path(logical_path)
    return TrashRecord(
        entry_id=str(entry_id),
        operation="delete",
        logical_path=rel,
        # 客户端把内容直接 rename 到 .lumi_trash/<trash_id>
        trash_path=f"{TRASH_DIRNAME}/{entry_id}",
        kind=str(kind or ("dir" if is_dir else "file")),
        is_dir=bool(is_dir),
        recursive=bool(recursive),
        bytes=int(bytes_size or 0),
        files=int(files or (0 if is_dir else 1)),
        dirs=int(dirs or (1 if is_dir else 0)),
        revision=str(revision or ""),
        created_at=stamp,
        deleted_at=stamp,
        job_id=str(getattr(context, "job_id", "") or ""),
        task_id=str(getattr(context, "job_id", "") or ""),
        conversation_id=str(getattr(context, "conversation_id", "") or ""),
        user_id=str(getattr(context, "user_id", "") or ""),
        device_id=str(getattr(context, "device_id", "") or ""),
        workspace_id=str(getattr(context, "workspace_id", "") or ""),
        retention_days=int(active.retention_days),
        expires_at=expires,
        restorable=True,
        reason=str(reason or ""),
    )


def is_index_path(rel_path: str) -> bool:
    rel = normalize_rel_path(rel_path)
    return rel in {f"{TRASH_DIRNAME}/{INDEX_NAME}", f"{TRASH_DIRNAME}/index.json"}


def guard_normal_operation(rel_path: str) -> str:
    """普通工作区操作（读/写/编辑/移动/删除）触碰回收站时的统一拒止原因。"""
    rel = normalize_rel_path(rel_path)
    if not is_trash_path(rel):
        return ""
    return (
        f"{OperationErrorCode.TRASH_PATH_FORBIDDEN.value}：{rel} 位于回收站 {TRASH_DIRNAME}/ 内，"
        "只能用 restore/purge/list_trash 接口访问（list 与 read 默认不可见）。"
    )


def expired_records(
    records: list[TrashRecord],
    *,
    policy: TrashPolicy | None = None,
    at: datetime | None = None,
) -> list[TrashRecord]:
    """超出保留期（可清理）的条目。"""
    active = policy or TrashPolicy()
    moment = at or datetime.now(timezone.utc)
    out: list[TrashRecord] = []
    for item in records:
        expires = _parse_iso(item.expires_at)
        if expires is None:
            # 老记录没有 expires_at：按删除时间 + 保留期推算，推算不出则视为过期。
            deleted = _parse_iso(item.deleted_at)
            expires = (
                deleted + timedelta(days=int(item.retention_days or active.retention_days))
                if deleted
                else moment - timedelta(seconds=1)
            )
        if expires <= moment:
            out.append(item)
    return out


def quota_plan(
    records: list[TrashRecord],
    *,
    incoming_bytes: int = 0,
    policy: TrashPolicy | None = None,
) -> dict[str, Any]:
    """判断加入一个新条目后是否超配额，并给出需要清理的最少条目。

    返回 ``{allowed, over_items, over_bytes, evictable, reason}``：``allowed=False``
    时调用方应要求审批/先清理，**不能**假装删除成功。
    """
    active = policy or TrashPolicy()
    total_items = len(records) + 1
    total_bytes = sum(int(item.bytes or 0) for item in records) + int(incoming_bytes or 0)
    over_items = total_items > int(active.max_items)
    over_bytes = total_bytes > int(active.max_bytes)
    evictable = expired_records(records, policy=active)
    reason = ""
    if over_items:
        reason = f"回收站条目数 {total_items} 超过上限 {active.max_items}"
    elif over_bytes:
        reason = (
            f"回收站占用 {total_bytes} 字节超过上限 {active.max_bytes} 字节"
        )
    return {
        "allowed": not (over_items or over_bytes),
        "over_items": bool(over_items),
        "over_bytes": bool(over_bytes),
        "evictable": [item.entry_id for item in evictable],
        "reason": reason,
        "items": total_items,
        "bytes": total_bytes,
    }


def restore_update(record: TrashRecord, *, revision: str = "") -> dict[str, Any]:
    """恢复成功后的记录变更（保留台账，便于"恢复后再撤销"与审计）。"""
    return {
        "restored_at": now_iso(),
        "restore_revision": str(revision or ""),
        "restorable": False,
    }


def summary_of(records: list[TrashRecord], *, policy: TrashPolicy | None = None) -> dict[str, Any]:
    """回收站概览（前端展示与"是否需要清理"判断都用它）。"""
    active = policy or TrashPolicy()
    expired = expired_records(records, policy=active)
    return {
        "dir": TRASH_DIRNAME,
        "index": f"{TRASH_DIRNAME}/{INDEX_NAME}",
        "count": len(records),
        "bytes": sum(int(item.bytes or 0) for item in records),
        "retention_days": int(active.retention_days),
        "max_items": int(active.max_items),
        "max_bytes": int(active.max_bytes),
        "expired": [item.entry_id for item in expired],
        "entries": [item.to_dict() for item in records[:50]],
    }


__all__ = [
    "INDEX_NAME",
    "INDEX_VERSION",
    "MAX_PURGE_BATCH",
    "TrashIndex",
    "TrashStore",
    "build_record",
    "expired_records",
    "guard_normal_operation",
    "is_index_path",
    "now_iso",
    "quota_plan",
    "restore_update",
    "summary_of",
]
