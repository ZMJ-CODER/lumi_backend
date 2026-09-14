"""``workspace.write@1`` 契约：写入请求的规范化参数与结果载荷。

实现重点（见方案 3）：路径解析 → 边界校验 → 动态保护 → ``expected_revision`` 校验 →
空内容策略 → 父目录 → 同目录临时文件 → fsync → 原子替换 → 生成新 revision → 审计。
本模块只定义**参数与载荷形状**；执行在 :mod:`app.workspace.write.operations`。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.operations.common import normalize_rel_path

#: 换行风格策略：``preserve``（默认，沿用已有文件风格）/ ``lf`` / ``crlf``。
NEWLINE_POLICIES: tuple[str, ...] = ("preserve", "lf", "crlf")
#: 文本文件判定用的可执行后缀（**只作为策略输入**，不当作完整安全检查）。
EXECUTABLE_SUFFIXES: tuple[str, ...] = (
    ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".exe", ".com", ".scr", ".js", ".command",
)


class WriteRequest(BaseModel):
    """一次写入的输入（身份字段一律不在参数里：由 ``OperationContext`` 注入）。"""

    model_config = ConfigDict(frozen=True)

    path: str = ""
    content: str = ""
    #: 覆盖已有文件时必须与当前 revision 一致（缺失则拒绝，避免盲写）。
    expected_revision: str = ""
    #: 空内容默认拒绝（防"模型把文件清空"），确需清空要显式打开。
    allow_empty: bool = False
    #: 自动创建父目录（需要记录 ``created_dirs`` 以便回滚/审计）。
    create_parents: bool = True
    #: 换行风格：默认沿用已有文件（新文件用 lf）。
    newline: str = "preserve"
    dry_run: bool = False

    @classmethod
    def from_arguments(cls, args: dict[str, Any]) -> "WriteRequest":
        payload = dict(args or {})
        return cls(
            path=normalize_rel_path(payload.get("path")),
            content=str(payload.get("content") if payload.get("content") is not None else ""),
            expected_revision=str(payload.get("expected_revision") or payload.get("revision") or ""),
            allow_empty=bool(payload.get("allow_empty")),
            create_parents=bool(payload.get("create_parents", True)),
            newline=str(payload.get("newline") or "preserve").casefold(),
            dry_run=bool(payload.get("dry_run")),
        )


class WriteChange(BaseModel):
    """写入的载荷（**不含正文**）。"""

    model_config = ConfigDict(frozen=True)

    path: str = ""
    created: bool = False
    bytes_written: int = 0
    previous_revision: str = ""
    revision: str = ""
    created_dirs: list[str] = Field(default_factory=list)
    newline: str = ""
    #: 可执行文件标记：只作为"要不要更严格审批"的策略输入。
    executable: bool = False
    added_lines: int = 0
    removed_lines: int = 0
    #: 写入是否走了"临时文件 + 原子替换"（Provider 能力决定，服务端如实记录）。
    atomic_replace: bool = True
    #: 文件权限/换行风格是否被保留或改写（Provider 层必须明确表态）。
    permissions_preserved: bool = True

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


__all__ = ["EXECUTABLE_SUFFIXES", "NEWLINE_POLICIES", "WriteChange", "WriteRequest"]
