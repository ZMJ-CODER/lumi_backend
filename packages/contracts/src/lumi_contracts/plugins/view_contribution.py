"""声明式视图贡献（``View Plugin`` 阶段的契约先冻结）。

第一版**只允许声明式视图**：插件给出 ``view_type`` + 数据 + Schema 版本 + 权限，
前端用官方组件渲染。绝不允许第三方注入 JSX/JS 到主渲染进程——这个决定必须体现在
契约里：``ViewContribution`` 没有"代码"字段，只有数据与渲染类型。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: 第一版支持的声明式视图类型（白名单，未知类型一律拒绝）。
VIEW_TYPES: frozenset[str] = frozenset(
    {"table", "chart", "diff", "timeline", "file_preview", "form"}
)

#: 单个视图贡献的数据上限（渲染是"展示"，不是搬运正文）。
VIEW_DATA_MAX_BYTES = 400_000


class ViewContribution(BaseModel):
    """一个声明式视图贡献（后端产出，前端用官方组件渲染）。"""

    model_config = ConfigDict(extra="forbid")

    view_type: str
    data: dict[str, Any] = Field(default_factory=dict)
    schema_version: int = Field(default=1, ge=1)
    #: 渲染该视图需要的权限提示（前端据此隐藏/置灰，不代替鉴权）。
    permissions: list[str] = Field(default_factory=list)
    title: str = ""
    #: 结果敏感度（沿用 CapabilityResult.sensitivity 词表）。
    sensitivity: str = ""
    #: 数据来源（能力名/Provider），便于前端显示"这份结果来自哪里"。
    source: str = ""
    plugin_id: str = ""

    @field_validator("view_type")
    @classmethod
    def _known_view(cls, value: str) -> str:
        text = str(value or "").strip().casefold()
        if text not in VIEW_TYPES:
            raise ValueError(
                f"未知视图类型：{value!r}（只允许 {sorted(VIEW_TYPES)}；自定义交互需隔离容器）"
            )
        return text

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "view_type": self.view_type,
            "schema_version": self.schema_version,
            "title": self.title,
            "permissions": list(self.permissions),
            "sensitivity": self.sensitivity,
            "source": self.source,
        }


__all__ = ["VIEW_DATA_MAX_BYTES", "VIEW_TYPES", "ViewContribution"]
