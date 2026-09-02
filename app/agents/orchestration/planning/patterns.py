"""半结构办公任务的声明式模式编译器。

模式编译属于规划阶段：它只把用户请求和已确认参数转换成任务节点，
不负责执行节点、持久化状态或处理副作用。
"""

from __future__ import annotations

import time
import uuid
from typing import Any


def _node(
    agent: str,
    name: str,
    params: dict[str, Any],
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": f"n{int(time.time())}-{uuid.uuid4().hex[:6]}",
        "name": name,
        "agent": agent,
        "params": params,
        "depends_on": list(depends_on or []),
    }


def build_pattern(
    pattern: str,
    request: str,
    params: dict[str, Any],
    office_docs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """按模式构造半结构任务 DAG。

    这里仅生成计划数据；未知模式返回空列表，由上层回退到自由规划。
    """
    normalized = str(pattern or "").strip().lower()
    docs = office_docs or []
    if normalized == "etl":
        return _build_etl(request, params, docs)
    if normalized == "router":
        return _build_router(request, params, docs)
    return []


def _build_etl(
    request: str,
    params: dict[str, Any],
    docs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reader(office_doc) → Transformer → Writer/产出。"""
    nodes: list[dict[str, Any]] = []
    reader_ids: list[str] = []
    for document in docs:
        doc_id = str(document.get("doc_id") or "")
        if not doc_id:
            continue
        node = _node(
            "office_doc",
            f"读取 {document.get('filename') or doc_id[:8]}",
            {"doc_id": doc_id, "instruction": request, "mode": "read"},
        )
        nodes.append(node)
        reader_ids.append(node["id"])

    writer_mode = str(params.get("writer") or "email")
    if writer_mode == "edit":
        doc_id = str(docs[0].get("doc_id") or "") if docs else ""
        nodes.append(
            _node(
                "office_doc",
                "写回文档",
                {"doc_id": doc_id, "instruction": request, "mode": "edit"},
                reader_ids,
            )
        )
    else:
        nodes.append(
            _node(
                "office_text",
                "产出结果",
                {
                    "instruction": request,
                    "task": str(params.get("task") or "extract"),
                },
                reader_ids,
            )
        )
    return nodes


def _build_router(
    request: str,
    params: dict[str, Any],
    docs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reader → Condition → 通知分支。"""
    nodes: list[dict[str, Any]] = []
    reader_ids: list[str] = []
    for document in docs:
        doc_id = str(document.get("doc_id") or "")
        if not doc_id:
            continue
        node = _node(
            "office_doc",
            f"读取并判断 {document.get('filename') or doc_id[:8]}",
            {
                "doc_id": doc_id,
                "instruction": f"按条件判断并提取相关信息：{request}",
                "mode": "analyze",
                "analyze_mode": "qa",
            },
        )
        nodes.append(node)
        reader_ids.append(node["id"])

    notify = str(params.get("notify") or "相关人员")
    nodes.append(
        _node(
            "office_text",
            f"通知 {notify}",
            {
                "instruction": f"把满足条件的结果整理成消息通知{notify}：{request}",
                "task": "email",
            },
            reader_ids,
        )
    )
    return nodes


def pattern_catalog_text() -> str:
    """返回供规划模型使用的模式目录。"""
    return (
        "可用模式：\n"
        "- etl：读文档/数据 → 转换处理 → 产出（写回文档或生成结果）。"
        "参数：task（extract/rewrite/summary）、writer（email/edit）\n"
        "- router：读文档并做条件判断 → 按结果通知/分发。参数：notify（通知对象）"
    )


__all__ = ["build_pattern", "pattern_catalog_text"]
