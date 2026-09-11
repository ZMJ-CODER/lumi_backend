"""工作区读取结果的**类型化 payload**（方案第二阶段：字节边界处完成转换）。

为什么需要它：``ExecutionResult.payload`` 应该是业务对象，而不是裸 dict。投影注册表
按 payload 的**类型名**挑选专用投影（``WorkspaceNavigatorResult``），如果 payload 停留
在 dict，专用投影永远不会被选中，正文又会退化成 JSON。

因此工作区读取类结果在适配边界（``app.contracts``）被转换成这里的类型，之后下游只见
``ExecutionResult[WorkspaceNavigatorResult]``。字段与现有 navigator 信封一一对应，
不新增语义、不改现有返回结构。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class WorkspaceSection(BaseModel):
    """一段正文（页/段/行/Sheet）。"""

    source: str = ""
    location: str = ""
    title: str = ""
    text: str = ""


class WorkspaceEntry(BaseModel):
    """目录条目（list 动作）。"""

    name: str = ""
    path: str = ""
    kind: str = "file"
    size: int | None = None
    modified_at: Any = None
    ext: str = ""
    ignored: bool = False


class WorkspaceMatch(BaseModel):
    """搜索命中（search 动作）。"""

    path: str = ""
    location: str = ""
    line: Any = None
    page: Any = None
    sheet: Any = None
    format: str = ""
    sensitive: bool = False
    # 命中上下文（上游已固定长度 + 脱敏）与命中类型必须一起进类型化 payload，
    # 否则模型/前端投影会拿到"有路径没有上下文"的命中。
    context: str = ""
    match_type: str = ""
    redacted: bool = False
    redaction_count: int = 0


class WorkspaceNavigatorResult(BaseModel):
    """``workspace_navigator`` 的业务 payload（类型化，供投影按类型识别）。"""

    status: str = ""
    action: str = ""
    summary: str = ""
    path: str = ""
    sections: list[WorkspaceSection] = Field(default_factory=list)
    entries: list[WorkspaceEntry] = Field(default_factory=list)
    matches: list[WorkspaceMatch] = Field(default_factory=list)
    has_more: bool = False
    cursor: str | None = None
    meta: dict = Field(default_factory=dict)
    error: dict | None = None

    @property
    def item_count(self) -> int:
        return len(self.sections) + len(self.entries) + len(self.matches)

    @classmethod
    def from_envelope(cls, envelope: Any) -> "WorkspaceNavigatorResult":
        """从 navigator 统一信封构造（信封形态是我们自己的契约，字段固定）。"""
        data = envelope if isinstance(envelope, dict) else {}
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        nested_meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        meta = {**(data.get("meta") if isinstance(data.get("meta"), dict) else {}), **nested_meta}
        # 分页事实以顶层为准（navigator 生产形态）；某些聚合路径把它们放在
        # ``data`` 里，这里做一次容错，避免分页信息在类型化时丢掉。
        has_more_raw = data.get("has_more", payload.get("has_more"))
        cursor_raw = data.get("cursor") or payload.get("cursor")
        return cls(
            status=str(data.get("status") or ""),
            action=str(data.get("action") or ""),
            summary=str(data.get("summary") or ""),
            path=str(payload.get("path") or ""),
            sections=[WorkspaceSection.model_validate(item) for item in (payload.get("sections") or [])
                      if isinstance(item, dict)],
            entries=[WorkspaceEntry.model_validate(item) for item in (payload.get("entries") or [])
                     if isinstance(item, dict)],
            matches=[WorkspaceMatch.model_validate(item) for item in (payload.get("matches") or [])
                     if isinstance(item, dict)],
            has_more=bool(has_more_raw),
            cursor=(str(cursor_raw) if cursor_raw else None),
            meta=meta,
            error=data.get("error") if isinstance(data.get("error"), dict) else None,
        )


__all__ = [
    "WorkspaceEntry",
    "WorkspaceMatch",
    "WorkspaceNavigatorResult",
    "WorkspaceSection",
]
