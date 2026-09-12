"""``workspace.edit@1`` 契约：严格匹配替换的参数与结果载荷。

必须防住的四件事（见方案 4）：

1. **读取后被并发修改** → 编辑前用 ``expected_revision`` 校验，编辑后重新读回校验版本；
2. **多处匹配导致全局替换** → 默认要求唯一匹配，多处即 ``OLD_TEXT_NOT_UNIQUE``；
3. **``old_str == new_str`` 产生无意义 revision** → 直接 ``no_change``；
4. **Agent 用过期上下文反复重试** → 返回最新 revision + ``safe_next_action``。

实现复用 write 的原子提交（客户端同一个写入通道），因此 ``code.edit`` 只是历史别名，
正式能力名统一为 ``workspace.edit@1``。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from app.contracts.operations.common import normalize_rel_path

#: ``occurrence`` 语义：``0`` = 必须唯一（默认）；``>0`` = 只替换第 N 处（1-based）。
DEFAULT_OCCURRENCE = 0


class EditRequest(BaseModel):
    """一次编辑的输入。"""

    model_config = ConfigDict(frozen=True)

    path: str = ""
    old_str: str = ""
    new_str: str = ""
    expected_revision: str = ""
    #: 0 = 要求唯一匹配；N>0 = 只替换第 N 处。
    occurrence: int = DEFAULT_OCCURRENCE
    #: 显式允许替换全部匹配（危险动作，必须由调用方明确要求）。
    replace_all: bool = False
    #: 匹配时是否对换行风格做归一（CRLF/LF 混用时仍能命中），默认开启并如实回报。
    normalize_newlines: bool = True
    dry_run: bool = False

    @classmethod
    def from_arguments(cls, args: dict[str, Any]) -> "EditRequest":
        payload = dict(args or {})
        try:
            occurrence = int(payload.get("occurrence") or 0)
        except (TypeError, ValueError):
            occurrence = DEFAULT_OCCURRENCE
        return cls(
            path=normalize_rel_path(payload.get("path")),
            old_str=str(payload.get("old_str") if payload.get("old_str") is not None else ""),
            new_str=str(payload.get("new_str") if payload.get("new_str") is not None else ""),
            expected_revision=str(payload.get("expected_revision") or payload.get("revision") or ""),
            occurrence=max(0, occurrence),
            replace_all=bool(payload.get("replace_all")),
            normalize_newlines=bool(payload.get("normalize_newlines", True)),
            dry_run=bool(payload.get("dry_run")),
        )

    @property
    def is_noop(self) -> bool:
        """旧内容与新内容完全一致 → 不应产生新 revision。"""
        return self.old_str == self.new_str


class EditChange(BaseModel):
    """编辑的载荷（**不含正文**；Diff 由受权限保护的接口按需获取）。"""

    model_config = ConfigDict(frozen=True)

    path: str = ""
    occurrences: int = 0
    old_revision: str = ""
    new_revision: str = ""
    added_lines: int = 0
    removed_lines: int = 0
    #: 匹配时是否做过换行归一（说明"为什么内容看起来一样却能命中"）。
    newline_normalized: bool = False
    encoding: str = "utf-8"
    #: 替换是否复用了原子提交路径。
    atomic_replace: bool = True

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


__all__ = ["DEFAULT_OCCURRENCE", "EditChange", "EditRequest"]
