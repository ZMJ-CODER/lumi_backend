"""工作区版本（revision）计算：**全系统唯一算法**。

为什么必须唯一：``workspace.write`` 返回的版本要给 ``workspace.edit`` / ``move`` /
``delete`` 用；只要有一处用了别的算法（Git hash、自增 ID、mtime），"版本对不上"就会变成
常态，调用方只能靠猜。

算法（见 :class:`~app.contracts.operations.common.RevisionRules`）：

* 文件：``sha256:<16 位内容哈希>:<字节数>``——内容变了版本必变，字节数补足元信息；
* 目录：``dir1:<16 位条目摘要>:<条目数>``——排序后的 ``名字/类型/大小/子版本`` 摘要，
  **不**递归哈希整棵目录（移动/删除目录时成本与收益不成比例）。

本模块只做"把客户端给的原文/条目转成版本"，不做 IO（IO 在操作网关里）。
"""

from __future__ import annotations

from typing import Any

from app.contracts.operations.common import RevisionRules

#: 被文本读取工具换行归一后的说明文案（避免调用方以为是内容不一致）。
NEWLINE_NOTE = "换行风格已归一（CRLF/LF 混用）"


def revision_for_text(text: str | bytes, *, size: int | None = None) -> str:
    """文本/字节内容的版本。"""
    return RevisionRules.for_file(text, size=size)


def revision_for_directory(entries: list[dict[str, Any]]) -> str:
    """目录快照版本（条目形如 ``{name, kind, size, revision}``）。"""
    return RevisionRules.for_directory(entries)


def revision_matches(expected: str, actual: str) -> bool:
    """``expected`` 是否代表 ``actual`` 这一次版本（支持完整串/裸哈希/>=8 位前缀）。"""
    return RevisionRules.matches(expected, actual)


def revision_problem(
    expected: str,
    actual: str,
    *,
    what: str = "文件",
) -> str:
    """版本校验：返回空串表示通过，否则返回可读的原因（不含正文）。"""
    if not str(expected or "").strip():
        return ""
    if revision_matches(expected, actual):
        return ""
    return (
        f"{what}已被改动：期望版本 {str(expected)[:48]}，当前版本 {str(actual)[:48]}"
        "（请重新读取后再提交，不要复用过期上下文）"
    )


def line_change_stats(before: str, after: str) -> tuple[int, int]:
    """粗略的新增/删除行数（用于 Diff 摘要；不做逐字 diff）。

    用 ``difflib.SequenceMatcher`` 的 opcodes 统计行级变化，结果有界、可解释：
    只回答"加了几行、删了几行"，不携带正文。
    """
    import difflib

    old_lines = str(before or "").splitlines()
    new_lines = str(after or "").splitlines()
    added = removed = 0
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in {"replace", "delete"}:
            removed += i2 - i1
        if tag in {"replace", "insert"}:
            added += j2 - j1
    return added, removed


def detect_newline(text: str) -> str:
    """判断主要换行风格：``crlf`` / ``lf``。"""
    raw = str(text or "")
    crlf = raw.count("\r\n")
    lf = raw.count("\n") - crlf
    return "crlf" if crlf > lf else "lf"


def apply_newline_policy(text: str, policy: str, *, existing: str = "") -> tuple[str, str]:
    """按策略统一换行，返回 ``(新文本, 实际使用的风格)``。

    ``preserve`` 沿用 ``existing`` 的风格（没有已有内容时用 lf）——这样"改写一行"不会
    因为编辑器风格差异把整个文件的行尾都改掉（否则 Diff 会不可读）。
    """
    wanted = str(policy or "preserve").casefold()
    if wanted == "preserve":
        wanted = detect_newline(existing) if existing else "lf"
    if wanted not in {"lf", "crlf"}:
        wanted = "lf"
    normalized = str(text or "").replace("\r\n", "\n")
    if wanted == "crlf":
        return normalized.replace("\n", "\r\n"), "crlf"
    return normalized, "lf"


def _normalize_for_match(text: str) -> str:
    return str(text or "").replace("\r\n", "\n")


def match_count(content: str, needle: str, *, normalize_newlines: bool = True) -> int:
    """``needle`` 在 ``content`` 中出现的次数（可选换行归一）。"""
    if not needle:
        return 0
    haystack = _normalize_for_match(content) if normalize_newlines else str(content or "")
    target = _normalize_for_match(needle) if normalize_newlines else str(needle)
    return haystack.count(target)


def replace_once(
    content: str,
    old_str: str,
    new_str: str,
    *,
    occurrence: int = 0,
    replace_all: bool = False,
    normalize_newlines: bool = True,
) -> tuple[str, int]:
    """严格匹配替换，返回 ``(新内容, 实际替换次数)``。

    调用方必须先确认匹配唯一性（``match_count``）；这里只负责**不越权替换**：
    ``replace_all=False`` 且 ``occurrence=0`` 时最多替换一处。
    """
    haystack = _normalize_for_match(content) if normalize_newlines else str(content or "")
    target = _normalize_for_match(old_str) if normalize_newlines else str(old_str)
    replacement = _normalize_for_match(new_str) if normalize_newlines else str(new_str)
    if not target:
        return haystack, 0
    if replace_all:
        return haystack.replace(target, replacement), haystack.count(target)
    if occurrence > 0:
        index = -1
        for _ in range(occurrence):
            index = haystack.find(target, index + 1)
            if index < 0:
                return haystack, 0
        return haystack[:index] + replacement + haystack[index + len(target):], 1
    index = haystack.find(target)
    if index < 0:
        return haystack, 0
    return haystack[:index] + replacement + haystack[index + len(target):], 1


__all__ = [
    "NEWLINE_NOTE",
    "apply_newline_policy",
    "detect_newline",
    "line_change_stats",
    "match_count",
    "replace_once",
    "revision_for_directory",
    "revision_for_text",
    "revision_matches",
    "revision_problem",
]
