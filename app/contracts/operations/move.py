"""``workspace.move@1`` 契约：移动/重命名的参数与结果载荷。

目录版本必须谨慎（见方案 5）：

* **文件移动**校验源文件的 ``expected_revision``（内容哈希）；
* **目录移动**校验目录快照版本（目录项摘要 ``dir1:<hash>:<count>``），
  **不**递归计算整棵目录内容哈希——那既慢又没必要；
* 移动成功后返回**新路径 + 操作版本**（目录移动返回新的目录快照版本）；
* 默认只支持同一文件系统的原子移动，跨设备一律 ``CROSS_DEVICE_UNSUPPORTED``，
  绝不自动"复制 + 删除"。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.operations.common import normalize_rel_path


class MoveRequest(BaseModel):
    """一次移动的输入。"""

    model_config = ConfigDict(frozen=True)

    source_path: str = ""
    target_path: str = ""
    #: 源版本：文件=内容版本；目录=目录快照版本（由 list 结果给出）。
    expected_revision: str = ""
    #: 目标已存在时的策略：默认拒绝（``ALREADY_EXISTS``），显式覆盖才允许。
    overwrite: bool = False
    create_parents: bool = True
    dry_run: bool = False

    @classmethod
    def from_arguments(cls, args: dict[str, Any]) -> "MoveRequest":
        payload = dict(args or {})
        return cls(
            source_path=normalize_rel_path(
                payload.get("source_path") or payload.get("from_path") or payload.get("path")
            ),
            target_path=normalize_rel_path(
                payload.get("target_path") or payload.get("to_path") or payload.get("destination")
            ),
            expected_revision=str(payload.get("expected_revision") or payload.get("revision") or ""),
            overwrite=bool(payload.get("overwrite")),
            create_parents=bool(payload.get("create_parents", True)),
            dry_run=bool(payload.get("dry_run")),
        )


class MoveChange(BaseModel):
    """移动的载荷（**不含正文**）。"""

    model_config = ConfigDict(frozen=True)

    source_path: str = ""
    target_path: str = ""
    #: 被移动对象类型：``file`` / ``dir``。
    kind: str = "file"
    #: 源版本（文件内容版本或目录快照版本）。
    source_revision: str = ""
    #: 移动后的版本（文件=新路径内容版本；目录=新路径目录快照版本）。
    target_revision: str = ""
    #: 目录移动时同时给出目录项摘要，便于后续用同一算法继续校验。
    directory_revision: str = ""
    renamed: bool = True
    entry_count: int = 0
    created_dirs: list[str] = Field(default_factory=list)
    #: 是否同一文件系统（跨设备一律拒绝，不自动复制+删除）。
    same_device: bool = True
    atomic: bool = True

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


__all__ = ["MoveChange", "MoveRequest"]
