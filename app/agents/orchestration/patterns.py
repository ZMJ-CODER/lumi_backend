"""模式库：半结构任务的常见编排模式（LLM 选模式 + 填参数，不从头画 DAG）.

内置模式：
  - etl：Reader → Transformer → Writer（读文档 → 转换 → 产出/写回）
  - router：Reader → Condition → 分支（通知 / 审批）
"""

from __future__ import annotations

import time
import uuid


def _node(agent: str, name: str, params: dict, depends_on: list[str] | None = None) -> dict:
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
    params: dict,
    office_docs: list[dict] | None = None,
) -> list[dict]:
    """按模式构造半结构任务 DAG."""
    pattern = str(pattern or "").strip().lower()
    docs = office_docs or []
    if pattern == "etl":
        return _build_etl(request, params, docs)
    if pattern == "router":
        return _build_router(request, params, docs)
    return []


def _build_etl(request: str, params: dict, docs: list[dict]) -> list[dict]:
    """Reader(office_doc analyze) → Transformer(office_text) → Writer(office_doc edit / office_text email)."""
    nodes = []
    reader_ids = []
    for d in docs:
        doc_id = str(d.get("doc_id") or "")
        if not doc_id:
            continue
        n = _node(
            "office_doc",
            f"读取 {d.get('filename') or doc_id[:8]}",
            {"doc_id": doc_id, "instruction": request, "mode": "read"},
        )
        nodes.append(n)
        reader_ids.append(n["id"])
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


def _build_router(request: str, params: dict, docs: list[dict]) -> list[dict]:
    """Reader → Condition(office_doc analyze) → 通知分支(office_text email)."""
    nodes = []
    reader_ids = []
    for d in docs:
        doc_id = str(d.get("doc_id") or "")
        if not doc_id:
            continue
        n = _node(
            "office_doc",
            f"读取并判断 {d.get('filename') or doc_id[:8]}",
            {
                "doc_id": doc_id,
                "instruction": f"按条件判断并提取相关信息：{request}",
                "mode": "analyze",
                "analyze_mode": "qa",
            },
        )
        nodes.append(n)
        reader_ids.append(n["id"])
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
    return (
        "可用模式：\n"
        "- etl：读文档/数据 → 转换处理 → 产出（写回文档或生成结果）。参数：task（extract/rewrite/summary）、writer（email/edit）\n"
        "- router：读文档并做条件判断 → 按结果通知/分发。参数：notify（通知对象）"
    )
