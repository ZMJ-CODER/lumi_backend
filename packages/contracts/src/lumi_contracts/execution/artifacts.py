"""产物引用与来源引用。

与旧契约（``app.agents.skills.output_contract``）保持**字段级一致**，方便第一阶段
双向重导出；新增 ``schema_name`` 让产物也能被投影按契约识别。
"""

from __future__ import annotations

from pydantic import BaseModel


class ArtifactRef(BaseModel):
    """后端产物引用；不允许把宿主路径或凭据暴露给模型。"""

    ref_id: str
    name: str = ""
    media_type: str = "application/octet-stream"
    size: int | None = None
    # 契约标识（可选）：形如 lumi.workspace_navigator.result@1
    schema_name: str = ""
    # 仅服务端可见的定位信息；投影阶段必须剥离。
    internal_locator: str = ""


class Citation(BaseModel):
    """可展示的来源定位信息，正文只保留短摘录。"""

    title: str = ""
    source: str = ""
    snippet: str = ""
    locator: str = ""


def artifact_refs_from(value: object) -> list[ArtifactRef]:
    """把遗留的产物引用（dict / 对象）归一为 ``ArtifactRef``。"""
    items = value if isinstance(value, (list, tuple)) else []
    out: list[ArtifactRef] = []
    for item in items:
        if isinstance(item, ArtifactRef):
            out.append(item)
            continue
        if isinstance(item, dict):
            payload = {key: item[key] for key in item if key in ArtifactRef.model_fields}
            if payload.get("ref_id"):
                out.append(ArtifactRef.model_validate(payload))
            continue
        ref_id = str(getattr(item, "ref_id", "") or "")
        if ref_id:
            out.append(ArtifactRef(
                ref_id=ref_id,
                name=str(getattr(item, "name", "") or ""),
                media_type=str(getattr(item, "media_type", "") or "application/octet-stream"),
                size=getattr(item, "size", None),
            ))
    return out


__all__ = ["ArtifactRef", "Citation", "artifact_refs_from"]
