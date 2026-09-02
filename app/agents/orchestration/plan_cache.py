"""按用户隔离、支持参数化的成功办公任务计划缓存。"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from app.agents.orchestration.models import TaskNode


# The cache contains executable planning decisions. Bump this whenever a
# planner-policy change could make an earlier DAG unsafe or semantically stale.
_VERSION = 2
_TTL_SECONDS = 7 * 86400
_FILE_TOKEN_RE = re.compile(
    r"(?iu)([a-z0-9_\-\u4e00-\u9fff][a-z0-9_.\-\u4e00-\u9fff]*\.[a-z0-9]{1,10})"
)
_ABSOLUTE_PATH_RE = re.compile(r"(?i)(?:[a-z]:[\\/]|/(?:app|tmp|var|home|root|workspace)/)")


def _request_pattern(request: str) -> str:
    text = _FILE_TOKEN_RE.sub("<file>", (request or "").strip().casefold())
    text = re.sub(r"\b\d+(?:\.\d+)?\b", "<number>", text)
    return re.sub(r"\s+", " ", text)[:1000]


def _document_signature(office_docs: list[dict] | None) -> list[str]:
    return sorted(
        f"{Path(str(doc.get('filename') or '')).name.casefold()}:{str(doc.get('mime_type') or doc.get('type') or '')}"
        for doc in office_docs or []
        if doc.get("filename") and doc.get("doc_id")
    )


def build_plan_cache_key(
    *,
    user_id: str,
    request: str,
    scene: str,
    user_role: str,
    office_docs: list[dict] | None,
    capability_signature: str,
) -> str:
    material = {
        "v": _VERSION,
        "user": hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:24],
        "intent": _request_pattern(request),
        "entities": _document_signature(office_docs),
        "scene": scene,
        "role": user_role,
        "capabilities": capability_signature,
    }
    digest = hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"agent:plan:v{_VERSION}:{digest}"


def _doc_maps(office_docs: list[dict] | None) -> tuple[dict[str, str], dict[str, str]]:
    by_id: dict[str, str] = {}
    by_token: dict[str, str] = {}
    ordered = sorted(
        (doc for doc in office_docs or [] if doc.get("doc_id")),
        key=lambda doc: Path(str(doc.get("filename") or "")).name.casefold(),
    )
    for index, doc in enumerate(ordered):
        token = f"{{{{office_doc:{index}}}}}"
        doc_id = str(doc["doc_id"])
        by_id[doc_id] = token
        by_token[token] = doc_id
    return by_id, by_token


def _replace(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {str(key): _replace(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace(item, replacements) for item in value]
    if isinstance(value, str):
        replaced = value
        for source, target in replacements.items():
            replaced = replaced.replace(source, target)
        return replaced
    return value


def _contains_internal_path(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_internal_path(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_internal_path(item) for item in value)
    return isinstance(value, str) and bool(_ABSOLUTE_PATH_RE.search(value))


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if any(secret in normalized for secret in ("api_key", "token", "password", "authorization")):
                return True
            if _contains_secret_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False


def encode_plan(nodes: list[TaskNode], office_docs: list[dict] | None, plan_text: str | None) -> dict | None:
    by_id, _ = _doc_maps(office_docs)
    filenames = [
        Path(str(doc.get("filename") or "")).name.casefold()
        for doc in office_docs or []
        if doc.get("doc_id")
    ]
    if len(filenames) != len(set(filenames)):
        return None
    encoded_nodes = []
    for node in nodes:
        params = _replace(node.params, by_id)
        if _contains_internal_path(params) or _contains_secret_key(params):
            return None
        encoded_nodes.append(
            {
                "id": node.id,
                "name": node.name,
                "agent": node.agent,
                "params": params,
                "depends_on": list(node.depends_on),
                "max_retries": node.max_retries,
                "approval": node.approval,
                "approval_note": node.approval_note,
            }
        )
    text_replacements = {
        str(doc["doc_id"]): Path(str(doc.get("filename") or "文档")).name
        for doc in office_docs or []
        if doc.get("doc_id")
    }
    safe_plan_text = _replace(plan_text, text_replacements) if plan_text else plan_text
    if _contains_internal_path(safe_plan_text):
        return None
    return {
        "version": _VERSION,
        "created_at": time.time(),
        "nodes": encoded_nodes,
        "plan_text": safe_plan_text,
    }


def decode_plan(payload: dict, office_docs: list[dict] | None) -> tuple[list[TaskNode], str | None] | None:
    if not isinstance(payload, dict) or payload.get("version") != _VERSION:
        return None
    filenames = [
        Path(str(doc.get("filename") or "")).name.casefold()
        for doc in office_docs or []
        if doc.get("doc_id")
    ]
    if len(filenames) != len(set(filenames)):
        return None
    _, by_token = _doc_maps(office_docs)
    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        return None
    old_to_new = {
        str(raw.get("id")): f"pc-{index + 1}-{uuid.uuid4().hex[:6]}"
        for index, raw in enumerate(raw_nodes)
        if isinstance(raw, dict) and raw.get("id")
    }
    nodes: list[TaskNode] = []
    try:
        for raw in raw_nodes:
            old_id = str(raw["id"])
            params = _replace(dict(raw.get("params") or {}), by_token)
            # A cached document plan is invalid when the current request does not
            # provide all document bindings used by the template.
            if "{{office_doc:" in json.dumps(params, ensure_ascii=False):
                return None
            nodes.append(
                TaskNode(
                    id=old_to_new[old_id],
                    name=str(raw.get("name") or raw.get("agent") or "任务步骤"),
                    agent=str(raw["agent"]),
                    params=params,
                    depends_on=[old_to_new[str(dep)] for dep in raw.get("depends_on") or []],
                    max_retries=int(raw.get("max_retries", 2)),
                    approval=bool(raw.get("approval", False)),
                    approval_note=str(raw.get("approval_note") or ""),
                )
            )
    except (KeyError, TypeError, ValueError):
        return None
    return nodes, str(payload.get("plan_text") or "") or None


class PlanCache:
    def __init__(self, ttl_seconds: int = _TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds

    async def get(self, key: str, office_docs: list[dict] | None):
        try:
            from app.core.redis import get_redis

            raw = await get_redis().get(key)
            if not raw:
                return None
            payload = json.loads(raw)
            decoded = decode_plan(payload, office_docs)
            if decoded is None:
                await get_redis().delete(key)
            return decoded
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取计划缓存失败: {}", exc)
            return None

    async def put(
        self,
        key: str,
        nodes: list[TaskNode],
        office_docs: list[dict] | None,
        plan_text: str | None,
    ) -> bool:
        payload = encode_plan(nodes, office_docs, plan_text)
        if payload is None:
            return False
        try:
            from app.core.redis import get_redis

            await get_redis().set(key, json.dumps(payload, ensure_ascii=False), ex=self.ttl_seconds)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("写入计划缓存失败: {}", exc)
            return False
