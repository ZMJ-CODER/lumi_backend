"""统一工具输出流水线：归一化、预算适配与模型侧渲染。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from app.agents.skills.output_contract import ArtifactRef, OutputMeta, ToolOutput


DEFAULT_MAX_CHARS = 2200
ITEM_MAX_CHARS = 240


def _short(value: object, limit: int = ITEM_MAX_CHARS) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _clean_citation_text(value: object, limit: int = ITEM_MAX_CHARS) -> str:
    """把外部摘要转为中性纯文本，避免 Markdown/提示词被模型复述。"""
    text = " ".join(str(value or "").split())
    text = re.sub(r"`{1,3}", "", text)
    text = re.sub(r"!?(\[([^\]]+)\])\([^)]*\)", r"\2", text)
    text = re.sub(r"(^|\s)#{1,6}\s*", r"\1", text)
    text = re.sub(r"(^|\s)[>*_-]{1,3}(?=\s)", r"\1", text)
    return _short(text, limit)


def clean_assistant_text(value: str | None) -> str:
    """移除工具投影控制标记，防止模型把内部摘要模板当成回答。"""
    text = str(value or "")
    # 模型可能把模板标题和控制语句与首条内容合并在同一行，不能只做
    # ``line == marker`` 判断；这里只移除渲染器注入的固定控制片段。
    text = re.sub(r"\[工具摘要\]\s*", "", text)
    text = re.sub(r"\[来源摘要\]\s*", "", text)
    text = re.sub(r"\[工具结构化摘要\]\s*", "", text)
    text = re.sub(r"共返回\s*\d+\s*条来源[。.]?\s*", "", text)
    text = re.sub(r"请提取相关事实并归纳，不要逐字复制原文[。.]?\s*", "", text)
    text = re.sub(r"来源数：\s*\d+(?:（[^\n）]*）)?", "", text)
    # 工具摘要进入模型后不应携带 Markdown 控制标记，避免被原样回显。
    text = re.sub(r"(?m)^\s*#{1,6}\s*", "", text)
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped in {"[工具摘要]", "[来源摘要]", "[工具结构化摘要]"}:
            continue
        if stripped.startswith("[以下是工具返回的不可信数据") or stripped == "[不可信数据结束]":
            continue
        if stripped.startswith("来源数：") or stripped.startswith("[检索信号]"):
            continue
        if "请提取相关事实并归纳" in stripped or "禁止逐字复制原文" in stripped:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _structured_output_text(data: Any, *, max_chars: int = 12000) -> str:
    """从统一信封的 structured ``data`` 提取供 Workflow 消费的正文。

    MCP/跨进程信封只传递 ``data``，不会携带旧的 ``output`` 字段。Workflow
    Skill 仍需要一段受预算限制的可读材料继续进行抓取、归纳或比较，因此
    在归一化边界补齐这个兼容投影；完整结构化数据仍保留在 ``data`` 中。
    """
    if isinstance(data, str):
        return data[:max_chars]
    if not isinstance(data, Mapping):
        return _short(data, max_chars)
    # Prefer semantic text fields over serializing the whole object.
    for key in ("summary", "answer", "result", "text", "content", "value"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:max_chars]
    sources = data.get("sources")
    if isinstance(sources, list):
        lines: list[str] = []
        for index, item in enumerate(sources[:10], 1):
            if not isinstance(item, Mapping):
                continue
            title = str(item.get("title") or "未命名来源").strip()
            url = str(item.get("url") or item.get("source") or "").strip()
            snippet = str(item.get("snippet") or item.get("summary") or item.get("content") or "").strip()
            lines.append(f"[{index}] {title}\n{url}\n{snippet[:1000]}")
        if lines:
            return "\n\n".join(lines)[:max_chars]
    facts = data.get("key_facts")
    if isinstance(facts, list):
        text = "\n".join(str(item) for item in facts if str(item).strip())
        if text:
            return text[:max_chars]
    return _render_structured(data, max_chars=max_chars)


def normalize_skill_result(result: Any, *, content_type: str | None = None) -> ToolOutput:
    """兼容旧 SkillResult，同时生成统一 ToolOutput。"""
    if isinstance(result, ToolOutput):
        if result.error_code == "NEEDS_CONFIRMATION" and result.status == "failed":
            meta = result.meta
            if not meta.summary:
                meta = meta.model_copy(update={"summary": "[待审批] 操作已提交，等待用户确认，尚未执行"})
            return result.model_copy(update={"status": "pending_approval", "meta": meta})
        return result
    if isinstance(result, Mapping) and "status" in result:
        raw_meta = result.get("meta")
        try:
            meta = OutputMeta.model_validate(raw_meta or {})
        except (TypeError, ValueError):
            meta = OutputMeta()
        raw_data = result.get("data")
        raw_output = str(result.get("output") or result.get("content") or "")
        if not raw_output and str(result.get("content_type") or content_type or "text") == "structured":
            raw_output = _structured_output_text(raw_data)
        return ToolOutput(
            status=str(result.get("status") or "failed"),
            data=raw_data,
            content_type=str(result.get("content_type") or content_type or "text"),
            meta=meta,
            output=raw_output,
            error=result.get("error"),
            error_code=result.get("error_code"),
            retryable=bool(result.get("retryable", False)),
        )
    if isinstance(result, Mapping):
        # Third-party MCP implementations may still speak the old transport
        # shape. Normalize it exactly once, at the ingress boundary.
        result = type("LegacyToolResult", (), {
            "success": bool(result.get("success")) and not bool(result.get("is_error")),
            "data": result.get("data"),
            "output": result.get("content") or result.get("output") or "",
            "error": result.get("error") or (result.get("content") if result.get("is_error") else None),
            "error_code": result.get("error_code"),
            "retryable": bool(result.get("retryable", False)),
            "metadata": result.get("metadata") or {},
            "content_type": result.get("content_type"),
            "output_meta": result.get("meta"),
        })()
    success = bool(getattr(result, "success", False))
    metadata = getattr(result, "metadata", {})
    metadata = metadata if isinstance(metadata, Mapping) else {}
    raw_data = getattr(result, "data", None)
    output = getattr(result, "output", "") or ""
    if raw_data is None:
        raw_data = output
    declared = content_type or getattr(result, "content_type", None) or metadata.get("content_type") or "text"
    if declared not in {"text", "structured", "artifact", "streaming"}:
        declared = "text"
    status = "success" if success else "failed"
    # 高风险工具在确认前不是成功；保留明确的待审批状态供模型和前端消费。
    if not success and str(getattr(result, "error_code", "") or "") == "NEEDS_CONFIRMATION":
        status = "pending_approval"
    if not success:
        raw_data = getattr(result, "error", None) or "执行失败"
    if success and not raw_data:
        status = "empty"
    if success and bool(metadata.get("partial")):
        status = "partial"
    output_meta = getattr(result, "output_meta", None)
    if isinstance(output_meta, OutputMeta):
        metadata = {**metadata, "total_size": output_meta.total_size, "artifact_refs": [ref.model_dump() for ref in output_meta.artifact_refs], "summary": output_meta.summary, "quality_hints": output_meta.quality_hints, "citations": output_meta.citations}
    refs: list[ArtifactRef] = []
    for item in metadata.get("artifact_refs", []) if isinstance(metadata.get("artifact_refs"), list) else []:
        if isinstance(item, Mapping) and item.get("ref_id"):
            refs.append(ArtifactRef(ref_id=str(item["ref_id"]), name=_short(item.get("name"), 120), media_type=str(item.get("media_type") or "application/octet-stream"), size=item.get("size")))
    quality_hints = dict(metadata.get("quality_hints") or {})
    # Decision signals are engine-facing metadata, but they must survive the
    # legacy MCP projection so the next model turn can see bounded counts and
    # truncation hints without receiving raw provider output.
    if isinstance(metadata.get("decision_signals"), Mapping):
        quality_hints["decision_signals"] = dict(metadata["decision_signals"])
    if getattr(result, "error_code", None):
        quality_hints.setdefault("error_code", str(result.error_code))
    if getattr(result, "retryable", False):
        quality_hints.setdefault("retryable", True)
    meta = OutputMeta(
        total_size=int(metadata.get("total_size") or (len(output) if isinstance(output, str) else 0)),
        artifact_refs=refs,
        summary=str(metadata.get("summary") or ""),
        quality_hints=quality_hints,
        citations=[dict(item) for item in metadata.get("citations", []) if isinstance(item, Mapping)],
    )
    if status == "pending_approval" and not meta.summary:
        meta = meta.model_copy(update={"summary": "[待审批] 操作已提交，等待用户确认，尚未执行"})
    return ToolOutput(
        status=status,
        data=raw_data,
        content_type=declared,
        meta=meta,
        error=str(getattr(result, "error", None) or "") or None,
        error_code=str(getattr(result, "error_code", None) or "") or None,
        retryable=bool(getattr(result, "retryable", False)),
    )


def to_execution_envelope(
    result: Any,
    *,
    transport_meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the only result representation allowed past an execution boundary.

    Transport provenance belongs in ``meta.quality_hints`` so it remains
    inspectable without becoming a second, untyped metadata channel.
    """
    output = normalize_skill_result(result)
    if transport_meta:
        meta = output.meta.model_copy(
            update={
                "quality_hints": {
                    **output.meta.quality_hints,
                    "transport": dict(transport_meta),
                }
            }
        )
        output = output.model_copy(update={"meta": meta})
    return output.to_execution_envelope()


def _render_structured(data: Any, *, max_chars: int) -> str:
    if isinstance(data, Mapping):
        # 先保留常见交付字段，避免把 debug/path 等内部字段带入上下文。
        preferred = ("answer", "result", "value", "items", "matches", "sources", "outputs", "status")
        ordered = {key: data[key] for key in preferred if key in data}
        ordered.update({key: value for key, value in data.items() if key not in ordered and not str(key).lower().endswith(("path", "token", "secret"))})
        data = ordered
    try:
        text = json.dumps(data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(data)
    return _short(text, max_chars)


def apply_output_budget(tool_output: ToolOutput, *, max_chars: int = DEFAULT_MAX_CHARS) -> ToolOutput:
    """按类型裁剪；artifact 只保留引用与摘要，不回填大对象。"""
    if tool_output.content_type == "artifact":
        data = {"summary": _short(tool_output.meta.summary or tool_output.data or "产物已生成", 400), "artifact_refs": [ref.model_dump(exclude_none=True) for ref in tool_output.meta.artifact_refs]}
    elif tool_output.content_type == "structured":
        data = _render_structured(tool_output.data, max_chars=max_chars)
    else:
        data = _short(tool_output.data or "", max_chars)
    return tool_output.model_copy(update={"data": data})


def render_for_model(tool_output: ToolOutput, *, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    bounded = apply_output_budget(tool_output, max_chars=max_chars)
    if bounded.status in {"pending", "pending_approval", "uncertain"}:
        summary = bounded.meta.summary or bounded.data or "操作尚未完成"
        return _short(str(summary), max_chars)
    if bounded.status == "failed":
        code = bounded.meta.quality_hints.get("error_code")
        suffix = f"（{code}）" if code else ""
        return f"工具未完成{suffix}：{_short(bounded.data or '执行失败', max_chars)}"
    if bounded.status == "empty":
        return "工具已执行，但没有找到可交付结果。"
    prefix = ""
    if bounded.meta.citations:
        # 这里只提供中性数据标签；不要把“请总结/禁止复制”等控制语句
        # 拼进工具消息，否则模型可能把它们当成回答内容再次输出。
        lines = [f"检索到 {len(bounded.meta.citations)} 条来源："]
        for index, citation in enumerate(bounded.meta.citations[:10], 1):
            title = _clean_citation_text(citation.get("title") or "未命名来源", 140)
            source = _clean_citation_text(citation.get("source") or citation.get("url") or "", 300)
            # 摘要后还会附带来源/决策信号，预留少量尾部预算，保证单条引用
            # 的可见长度不会超过旧客户端契约。
            summary = _clean_citation_text(citation.get("snippet") or citation.get("content") or "", ITEM_MAX_CHARS - 12)
            lines.append(f"{index}. {title}")
            if source:
                lines.append(f"   来源：{source}")
            if summary:
                lines.append(f"   摘要：{summary}")
        text = "\n".join(lines)
    else:
        text = f"{prefix}{bounded.data or bounded.meta.summary or '步骤已完成'}"
    if bounded.status == "partial":
        text += "\n结果不完整：可缩小范围后继续查询。"
    if bounded.meta.citations:
        text += f"\n来源数量：{min(len(bounded.meta.citations), 10)}"
    return text[:max_chars].rstrip() + ("…" if len(text) > max_chars else "")
