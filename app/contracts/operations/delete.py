"""``workspace.delete@1`` 契约 + 回收站记录（默认可恢复，永久删除必须高危审批）。

默认流程（见方案 6）：权限校验 → 路径保护 → 影响面统计 → dry_run 或审批判断 →
移入工作区回收站 → 生成恢复记录 → 返回统计。

约束：

* ``recursive`` 默认 False，非空目录必须显式递归；
* 影响面超过阈值 → 转审批（不是失败）；
* ``permanent=True`` 属高危，必须审批；
* 删除符号链接**本身**，不跟随链接内容；
* 路径不存在 → ``already_absent``（幂等成功态）；
* 回收站移动失败 → **绝不能**报告删除成功。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.operations.common import normalize_rel_path


class DeleteRequest(BaseModel):
    """一次删除的输入。"""

    model_config = ConfigDict(frozen=True)

    path: str = ""
    recursive: bool = False
    #: True = 真删除（不可恢复）；默认 False = 移入回收站。
    permanent: bool = False
    expected_revision: str = ""
    dry_run: bool = False

    @classmethod
    def from_arguments(cls, args: dict[str, Any]) -> "DeleteRequest":
        payload = dict(args or {})
        return cls(
            path=normalize_rel_path(payload.get("path")),
            recursive=bool(payload.get("recursive")),
            permanent=bool(payload.get("permanent")),
            expected_revision=str(payload.get("expected_revision") or payload.get("revision") or ""),
            dry_run=bool(payload.get("dry_run")),
        )


class DeleteStats(BaseModel):
    """影响面统计（审批与前端展示都以此为准）。"""

    model_config = ConfigDict(frozen=True)

    files: int = 0
    dirs: int = 0
    bytes: int = 0
    #: 被删对象的类型：``file`` / ``dir``（前端按类型给不同提示）。
    kind: str = "file"
    recursive: bool = False
    permanent: bool = False
    to_trash: bool = True
    entry_id: str = ""
    #: 展示用条目（有界；绝对路径一律不出现）。
    entries: list[str] = Field(default_factory=list)
    restorable: bool = False
    retention_days: int = 0
    is_symlink: bool = False

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class TrashRecord(BaseModel):
    """回收站记录：原始路径、任务、删除时间与恢复信息（恢复接口的唯一依据）。

    与客户端 ``.lumi_trash/trash.json`` 的字段一一对应（``trash_id`` ↔ ``entry_id``），
    这样工作区面板与后端看到的是**同一份**记录。
    """

    model_config = ConfigDict(frozen=True)

    entry_id: str = ""
    operation: str = "delete"
    #: 被删前的原始工作区相对路径（恢复回这里）。
    logical_path: str = ""
    #: 内容当前落点（客户端 rename 目标：``.lumi_trash/<trash_id>``）。
    trash_path: str = ""
    kind: str = "file"
    is_dir: bool = False
    recursive: bool = False
    bytes: int = 0
    files: int = 0
    dirs: int = 0
    revision: str = ""
    created_at: str = ""
    deleted_at: str = ""
    #: 任务/会话归属：清理由同一会话/任务完成，避免误删别人的回收站条目。
    job_id: str = ""
    task_id: str = ""
    conversation_id: str = ""
    user_id: str = ""
    device_id: str = ""
    workspace_id: str = ""
    #: 保留期与恢复信息。
    retention_days: int = 7
    expires_at: str = ""
    restorable: bool = True
    restored_at: str = ""
    restore_revision: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class DeleteChange(BaseModel):
    """删除的载荷。"""

    model_config = ConfigDict(frozen=True)

    path: str = ""
    stats: DeleteStats = Field(default_factory=DeleteStats)
    record: TrashRecord | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "path": self.path,
            "stats": self.stats.to_dict(),
        }
        if self.record is not None:
            payload["record"] = self.record.to_dict()
        return payload


__all__ = ["DeleteChange", "DeleteRequest", "DeleteStats", "TrashRecord"]
