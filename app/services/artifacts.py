"""产物（Artifact）标识与受权限保护的下载解析。

背景（方案 §二 的 `artifact_created`）：事件里**只给引用与元数据**
（``artifact_id`` / ``filename`` / ``mime_type`` / ``size_bytes`` / ``expires_at``），
绝不下发下载地址或令牌；前端点击后再走受权限保护的下载接口。

设计取舍：

* **不新建产物存储**：产物字节仍由既有通用产物目录持有
  （``app/services/office_docs.py::generic_outputs_dir`` /
  ``resolve_generic_output``，已内含按用户隔离 + 路径越权校验）；
* ``artifact_id`` 是**签名的不透明标识**（HMAC + base64url），内容是
  ``{container_id, name, issued_at}``：不需要新的数据库/Redis 表，且无法被伪造
  成任意路径；真正的授权仍由"当前登录用户 + 该用户的产物目录"决定；
* 过期时间由签发时间 + TTL 决定，不需要额外状态；
* 引用里的 ``internal_locator`` 永不外发（契约已要求，这里同样不参与投影）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any

from loguru import logger

#: 产物在服务端的保留期（与回收站 7 天一致；过期后下载返回 404 + 提示重新生成）。
ARTIFACT_TTL_DAYS = 7
ARTIFACT_TTL_SECONDS = ARTIFACT_TTL_DAYS * 24 * 3600

_PREFIX = "art_"
_SIG_CHARS = 16

#: 后缀 → MIME（只覆盖常见交付物；未知一律 application/octet-stream）。
_MIME_BY_SUFFIX: dict[str, str] = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".html": "text/html",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".zip": "application/zip",
}


def _signing_key() -> bytes:
    """签名密钥：复用服务端密钥并加命名空间，避免与 JWT 交叉使用。"""
    try:
        from app.core.config import settings

        secret = str(getattr(settings, "JWT_SECRET_KEY", "") or "")
    except Exception:  # noqa: BLE001
        secret = ""
    if not secret:
        secret = "lumi-artifact-fallback-key"
    return hashlib.sha256(f"lumi:artifact:{secret}".encode("utf-8")).digest()


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def media_type_for(name: str) -> str:
    return _MIME_BY_SUFFIX.get(Path(str(name or "")).suffix.lower(), "application/octet-stream")


def make_artifact_id(container_id: str, name: str, *, issued_at: float | None = None) -> str:
    """生成签名产物标识（同一产物在同一秒内是稳定的，便于去重）。"""
    payload = {
        "c": str(container_id or "")[:120],
        "n": Path(str(name or "")).name[:200],
        "t": int(issued_at if issued_at is not None else time.time()),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(_signing_key(), raw, hashlib.sha256).hexdigest()[:_SIG_CHARS]
    return f"{_PREFIX}{_b64encode(raw)}.{signature}"


def parse_artifact_id(artifact_id: str) -> dict[str, Any] | None:
    """解析并校验签名；形状不对/签名不符一律返回 ``None``（不抛错）。"""
    text = str(artifact_id or "").strip()
    if not text.startswith(_PREFIX) or "." not in text:
        return None
    body, _, signature = text[len(_PREFIX):].rpartition(".")
    if not body or len(signature) != _SIG_CHARS:
        return None
    try:
        raw = _b64decode(body)
    except (ValueError, TypeError):
        return None
    expected = hmac.new(_signing_key(), raw, hashlib.sha256).hexdigest()[:_SIG_CHARS]
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or not payload.get("c") or not payload.get("n"):
        return None
    return {
        "container_id": str(payload.get("c") or ""),
        "name": Path(str(payload.get("n") or "")).name,
        "issued_at": int(payload.get("t") or 0),
    }


def expires_at_for(issued_at: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(max(0.0, float(issued_at)) + ARTIFACT_TTL_SECONDS, tz=timezone.utc).isoformat()


def artifact_path(user_id: str, artifact_id: str) -> Path | None:
    """受权限保护地解析产物路径（归属 + 存在性 + 有效期 + 路径越权校验）。"""
    parsed = parse_artifact_id(artifact_id)
    if parsed is None:
        return None
    issued_at = int(parsed.get("issued_at") or 0)
    # 缺失签发时间视为已过期（fail-closed）：否则伪造/截断的时间戳能让产物永不过期。
    if issued_at <= 0 or time.time() > issued_at + ARTIFACT_TTL_SECONDS:
        return None
    from app.services.office_docs import resolve_generic_output

    # resolve_generic_output 只在该用户自己的产物目录里解析，等价于归属校验。
    return resolve_generic_output(user_id, parsed["container_id"], parsed["name"])


def artifact_record(user_id: str, artifact_id: str) -> dict[str, Any] | None:
    """产物元数据（不含字节、不含服务端路径）。"""
    parsed = parse_artifact_id(artifact_id)
    path = artifact_path(user_id, artifact_id)
    if parsed is None or path is None:
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None
    return {
        "artifact_id": artifact_id,
        "filename": path.name,
        "mime_type": media_type_for(path.name),
        "size_bytes": size,
        "type": path.suffix.lstrip(".").lower(),
        "expires_at": expires_at_for(parsed["issued_at"]),
    }


def artifact_from_output(container_id: str, item: dict[str, Any]) -> dict[str, Any] | None:
    """任务产物条目（``{"name", "size"}``）→ 事件用产物引用（安全字段）。"""
    name = Path(str(item.get("name") or "")).name
    if not name:
        return None
    issued_at = time.time()
    return {
        "artifact_id": make_artifact_id(container_id, name, issued_at=issued_at),
        "filename": name[:200],
        "mime_type": media_type_for(name),
        "size_bytes": int(item.get("size") or 0),
        "type": Path(name).suffix.lstrip(".").lower()[:20],
        "expires_at": expires_at_for(issued_at),
    }


def artifacts_for_job(user_id: str, job: Any) -> list[dict[str, Any]]:
    """从既有 Job 快照派生产物引用（只读 ``node.result["outputs"]``，不读正文）。

    与 ``office_task_memory._artifact_refs`` 同源，但产出的是**事件/快照用**的
    引用形状（带 ``artifact_id``），因此刷新后仍能恢复 Artifact 卡片。
    """
    container_id = str(getattr(job, "job_id", "") or "")
    if not container_id:
        return []
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for node in getattr(job, "nodes", []) or []:
        status = getattr(getattr(node, "status", None), "value", getattr(node, "status", ""))
        if str(status) != "completed":
            continue
        result = getattr(node, "result", None)
        if not isinstance(result, dict):
            continue
        for item in result.get("outputs") or []:
            if not isinstance(item, dict):
                continue
            ref = artifact_from_output(container_id, item)
            if ref is None or ref["artifact_id"] in seen:
                continue
            seen.add(ref["artifact_id"])
            out.append(ref)
    return out[:50]


def validate_artifacts(user_id: str, refs: Any) -> list[dict[str, Any]]:
    """校验引用是否仍然可下载（过期/被清理的产物不得出现在恢复快照里）。"""
    valid: list[dict[str, Any]] = []
    for raw in refs or []:
        if not isinstance(raw, dict):
            continue
        artifact_id = str(raw.get("artifact_id") or "")
        record = artifact_record(user_id, artifact_id) if artifact_id else None
        if record is None:
            logger.debug("[artifact] 引用已失效（过期或已清理）: {}", artifact_id[:24])
            continue
        valid.append(record)
    return valid


__all__ = [
    "ARTIFACT_TTL_DAYS",
    "ARTIFACT_TTL_SECONDS",
    "artifact_from_output",
    "artifact_path",
    "artifact_record",
    "artifacts_for_job",
    "expires_at_for",
    "make_artifact_id",
    "media_type_for",
    "parse_artifact_id",
    "validate_artifacts",
]
