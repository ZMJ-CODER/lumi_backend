"""用户工作区注册与绑定（纯元数据，不保存工作区文件）。

工作区内容由 Electron 本地工作区持有：agent 通过桌面 MCP（workspace_read /
workspace_stage_write / workspace_diff / workspace_commit 等）读写本地文件，
服务端不再保存 ``workspaces/<user>/<workspace_id>/staging`` 文件副本，也不再
在 manifest 里维护与 Electron 重复的 ``document_refs``。

本模块只保留需要"服务端权威"的部分：
  - workspace_id 的签发；
  - conversation_id -> workspace_id 一对一绑定（会话是用户侧选择器）；
  - user_id 归属与存在性校验（一切路径都以用户目录为界）；
  - 工作区注册在哪个桌面设备（device_id）及对应的 MCP server 名
    （device_server），供后端把任务路由回持有该工作区的 Electron。

每个工作区在磁盘上只有一个 ``manifest.json`` 元数据文件：
  {workspace_id, name, conversation_id, device_id, device_server, created_at}
不含任何文件内容、文档引用或解析状态。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import settings
from app.services.safe_delete import UnsafeRemoval, remove_tree

ROOT = Path(settings.UPLOAD_DIR).parent / "workspaces"

# manifest 结构版本：v2 = 纯注册元数据（无 staging、无 document_refs），
# v3 = 增加设备注册字段（device_id / device_server）。
_SCHEMA_VERSION = 3


def _safe(value: str) -> str:
    return "".join(c for c in str(value or "") if c.isalnum() or c in "-_.")[:80]


def _root(user_id: str, workspace_id: str) -> Path:
    return ROOT / _safe(user_id) / _safe(workspace_id)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _manifest(user_id: str, workspace_id: str) -> tuple[Path, dict]:
    """Read the metadata manifest; normalise legacy manifests in place."""
    root = _root(user_id, workspace_id)
    marker = root / "manifest.json"
    try:
        meta = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LookupError("工作区不存在或清单不可用") from exc
    if not isinstance(meta, dict):
        raise LookupError("工作区清单不可用")
    # Legacy manifests may carry a server-side document_refs mirror from the
    # era when the backend stored workspace files.  It is not authoritative:
    # Electron owns workspace document references now, so it is ignored here
    # and dropped on the next write.
    meta.pop("document_refs", None)
    meta.setdefault("workspace_id", workspace_id)
    meta.setdefault("conversation_id", None)
    meta.setdefault("device_id", None)
    meta.setdefault("device_server", None)
    meta.setdefault("created_at", _now())
    return marker, meta


def create_workspace(
    user_id: str,
    name: str,
    conversation_id: str | None = None,
    *,
    device_id: str | None = None,
    device_server: str | None = None,
) -> dict:
    """Create a workspace registration; no server-side file mirror is created."""
    wid = uuid.uuid4().hex[:16]
    root = _root(user_id, wid)
    try:
        root.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:  # pragma: no cover - uuid collision is theoretical
        raise LookupError("工作区创建冲突，请重试") from exc
    meta = {
        "schema_version": _SCHEMA_VERSION,
        "workspace_id": wid,
        "name": (name or "未命名项目")[:200],
        "conversation_id": str(conversation_id or "")[:128] or None,
        "device_id": str(device_id or "")[:80] or None,
        "device_server": str(device_server or "")[:80] or None,
        "created_at": _now(),
    }
    (root / "manifest.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def register_workspace_device(
    user_id: str,
    workspace_id: str,
    *,
    device_id: str,
    device_server: str | None = None,
    conversation_id: str | None = None,
) -> dict:
    """Record the desktop device that hosts this workspace (metadata only).

    ``device_server`` is the MCP server row name for that device so later
    conversation → workspace → device routing can pick the right Electron
    connection.  Re-registering from the same device is idempotent; switching
    device is allowed and simply records the new host.
    """
    marker, meta = _manifest(user_id, workspace_id)
    did = str(device_id or "").strip()[:80]
    if not did:
        raise ValueError("缺少 device_id")
    meta["device_id"] = did
    if device_server is not None:
        server = str(device_server).strip()[:80]
        meta["device_server"] = server or None
    if conversation_id:
        cid = str(conversation_id or "").strip()
        if cid:
            existing = str(meta.get("conversation_id") or "").strip()
            if existing and existing != cid:
                raise ValueError("该工作区已经绑定到另一个对话")
            other = workspace_for_conversation(user_id, cid)
            if other and str(other.get("workspace_id") or "") != str(workspace_id):
                raise ValueError("该对话已经绑定到另一个工作区")
            meta["conversation_id"] = cid
    marker.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def get_workspace(user_id: str, workspace_id: str) -> dict:
    """Return metadata for a workspace owned by ``user_id`` or raise LookupError."""
    _marker, meta = _manifest(user_id, workspace_id)
    return meta


def ensure_workspace(user_id: str, workspace_id: str) -> Path:
    """Permission/existence gate: raise LookupError when the user does not own it."""
    root = _root(user_id, workspace_id)
    if not (root / "manifest.json").is_file():
        raise LookupError("工作区不存在")
    return root


def list_workspaces(user_id: str) -> list[dict]:
    """List every workspace registration owned by ``user_id``."""
    root = ROOT / _safe(user_id)
    items: list[dict] = []
    if not root.is_dir():
        return items
    for p in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        marker = p / "manifest.json"
        if not marker.is_file():
            continue
        try:
            meta = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(meta, dict):
            # Never leak the legacy server-side document_refs mirror to clients:
            # Electron owns workspace document references.
            meta.pop("document_refs", None)
            items.append(meta)
    return items


def workspace_for_conversation(user_id: str, conversation_id: str | None) -> dict | None:
    """Resolve the single workspace owned by a conversation.

    The conversation is the user-facing selector.  This lookup is server-side
    and user-scoped; callers must not ask the client to guess a workspace ID.
    """
    cid = str(conversation_id or "").strip()
    if not cid:
        return None
    for meta in list_workspaces(user_id):
        if str(meta.get("conversation_id") or "") == cid:
            return meta
    return None


def bind_workspace_to_conversation(user_id: str, workspace_id: str, conversation_id: str) -> dict:
    """Bind one workspace to one conversation, migrating pre-binding manifests."""
    marker, meta = _manifest(user_id, workspace_id)
    cid = str(conversation_id or "").strip()
    if not cid:
        raise ValueError("缺少 conversation_id")
    existing = str(meta.get("conversation_id") or "").strip()
    if existing and existing != cid:
        raise ValueError("该工作区已经绑定到另一个对话")
    other = workspace_for_conversation(user_id, cid)
    if other and str(other.get("workspace_id") or "") != str(workspace_id):
        raise ValueError("该对话已经绑定到另一个工作区")
    meta["conversation_id"] = cid
    marker.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def delete_workspace(user_id: str, workspace_id: str) -> dict:
    """Remove a workspace registration and any legacy server-side file mirror.

    Only a directory that carries this user's manifest may be removed.  This
    also deletes stale ``staging/`` / ``snapshots/`` leftovers written by the
    old server-file model, which Electron no longer needs.
    """
    root = _root(user_id, workspace_id)
    marker = root / "manifest.json"
    if not marker.is_file():
        raise LookupError("工作区不存在")
    try:
        # 受控删除：边界 = 该用户的工作区根，越界即拒绝（而不是"尽力删"）。
        remove_tree(root.parent, root)
    except (OSError, UnsafeRemoval) as exc:  # pragma: no cover - best effort cleanup
        raise LookupError("工作区清理失败") from exc
    return {"workspace_id": workspace_id, "deleted": True}
