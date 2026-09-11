"""统一工具输出流水线：归一化、预算适配与模型侧渲染。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from loguru import logger

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
    if isinstance(result, Mapping) and "payload" in result and "status" in result:
        # 新契约形态（ExecutionResult dict）：先经适配器解析（payload → data），
        # 否则下面按 ``data`` 读取会把有 payload 的结果误判成 empty。
        try:
            from app.contracts import execution_result_from_envelope, to_tool_output

            return to_tool_output(execution_result_from_envelope(result))
        except Exception as exc:  # noqa: BLE001 - 回退到宽松归一，但留下审计标记
            _note_contract_violation(exc)
            relaxed = dict(result)
            relaxed.setdefault("data", relaxed.get("payload"))
            result = relaxed
    if isinstance(result, Mapping) and "status" in result:
        raw_meta = result.get("meta")
        try:
            meta = OutputMeta.model_validate(raw_meta or {})
        except (TypeError, ValueError):
            meta = OutputMeta()
        raw_data = result.get("data")
        raw_output = str(result.get("output") or result.get("content") or "")
        declared = str(result.get("content_type") or content_type or "text")
        if declared not in {"text", "structured", "artifact", "streaming"}:
            # 未知 content_type 不能直接构造 ToolOutput（Literal 校验会抛），
            # 也不能让结果消失：退化为 text，与通用分支保持一致。
            declared = "text"
        if not raw_output and declared == "structured":
            raw_output = _structured_output_text(raw_data)
        return ToolOutput(
            status=str(result.get("status") or "failed"),
            call_id=str(result.get("call_id") or "") or None,
            data=raw_data,
            content_type=declared,
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


_CONTRACT_VIOLATIONS: dict[str, int] = {}


def contract_violation_report() -> dict[str, int]:
    """契约适配失败的计数（按错误码），用于告警/验收观察。"""
    return dict(_CONTRACT_VIOLATIONS)


def _note_contract_violation(exc: BaseException) -> dict[str, Any]:
    """记录一次契约适配失败：可观测（warn + 计数）且可审计（随结果带标记）。

    不能静默降级：方案第六条要求"无法无损转换时返回
    UNSUPPORTED_CONTRACT_VERSION"，因此这里既告警又把标记挂到结果元数据上，
    下游/审计能看到"这个结果走了兼容归一"。
    """
    code = str(getattr(exc, "code", "") or type(exc).__name__)
    _CONTRACT_VIOLATIONS[code] = _CONTRACT_VIOLATIONS.get(code, 0) + 1
    marker = {
        "code": code,
        "message": str(exc)[:200],
        "fallback": "legacy_normalizer",
        "count": _CONTRACT_VIOLATIONS[code],
    }
    if _CONTRACT_VIOLATIONS[code] == 1:
        logger.warning("执行信封契约适配失败，已回退旧归一（并标记审计）: {}", marker["message"])
    else:
        logger.debug("执行信封契约适配失败（第 {} 次）: {}", _CONTRACT_VIOLATIONS[code], marker["message"])
    return marker


def normalize_execution_envelope(value: Any, *, tool_name: str = "") -> ToolOutput:
    """跨进程执行信封 → ``ToolOutput``（第二阶段：边界只走契约适配器）。

    ``call_skill`` 之后拿到的已经是信封字典；这里用
    ``LegacyEnvelopeAdapter.adapt_envelope`` 解析一次，裸字典不再向下游扩散，
    也无法再靠 ``isinstance(x, Mapping) and "status" in x`` 之类的猜测处理。

    契约适配失败（信封形状不合法、未知 ``content_type`` 等）时回退到旧的宽松
    归一：信封出问题不能让结果消失；同时**告警 + 在结果上打审计标记**，
    不做静默降级。
    """
    if isinstance(value, ToolOutput):
        return normalize_skill_result(value)
    try:
        from app.contracts import ExecutionResult, execution_result_from_envelope, to_tool_output

        result = value if isinstance(value, ExecutionResult) else execution_result_from_envelope(value, tool_name=tool_name)
        output = to_tool_output(result)
    except Exception as exc:  # noqa: BLE001 - 契约层异常不得丢结果
        marker = _note_contract_violation(exc)
        fallback = normalize_skill_result(value)
        meta = fallback.meta.model_copy(
            update={"quality_hints": {**fallback.meta.quality_hints, "contract_violation": marker}}
        )
        return fallback.model_copy(update={"meta": meta})
    if output.content_type == "structured" and not output.output:
        # 与旧的 dict 归一保持一致：结构化正文补一份可读文本，方便下游直接引用。
        output = output.model_copy(update={"output": _structured_output_text(output.data)})
    return output


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


def _has_structured_sections(tool_output: ToolOutput) -> bool:
    """结果是否是"列表型"形态（工作区读取/检索/列举）。

    三种形态都必须走契约投影，否则会出现"正文走投影、命中/目录走 JSON"的绕过：

    * ``sections``：读取正文分段；
    * ``matches``：检索命中（路径 + 位置 + 固定长度上下文）；
    * ``entries``：目录列举。

    其他结构化 payload 保持既有 JSON 渲染行为不变。
    """
    data = tool_output.data
    if not isinstance(data, dict):
        return False
    for source in (data, data.get("data")):
        if not isinstance(source, dict):
            continue
        for key in ("sections", "matches", "entries"):
            value = source.get(key)
            if isinstance(value, list) and value:
                return True
    return False


def _contract_model_text(tool_output: ToolOutput, *, max_chars: int) -> str:
    """经契约层渲染模型文本（第二阶段：投影统一走 ``lumi_contracts``）。

    注意**必须传原始 ToolOutput**：``apply_output_budget`` 会把 ``data`` 序列化成
    JSON 字符串，之后投影就再也看不到 ``sections`` 结构了。

    * 传输边界只在适配器内部接触裸字典：``to_execution_result()`` 之后下游只见
      ``ExecutionResult``；
    * 任何契约层失败都回退到旧的通用渲染，**不能因为投影问题丢结果**。
    """
    try:
        from app.contracts import (
            is_workspace_envelope,
            projection_registry,
            to_execution_result,
            to_workspace_result,
        )

        metadata = tool_output.metadata if isinstance(tool_output.metadata, dict) else {}
        tool_name = str(metadata.get("tool") or "")
        # 工作区读取结果走**类型化**适配（裸字典留在适配器内部），
        # 这样投影层能按 payload 类型名挑到专用投影。
        result = (
            to_workspace_result(tool_output)
            if is_workspace_envelope(tool_output)
            else to_execution_result(tool_output, tool_name=tool_name)
        )
        registry = projection_registry(model_budget=max(200, int(max_chars)))
        view = registry.project("model", result)
        return str(view.get("text") or "")
    except Exception as exc:  # noqa: BLE001 - 契约层异常不得影响结果交付
        logger.debug("契约投影失败，回退通用渲染: {}", str(exc)[:160])
        return ""


def render_for_model(tool_output: ToolOutput, *, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    # 分段正文（工作区读取）在预算裁剪前就用契约投影渲染成可读文本，
    # 避免正文被序列化成 JSON 后埋进 meta/limits 噪音。
    structured_text = (
        _contract_model_text(tool_output, max_chars=max_chars)
        if _has_structured_sections(tool_output)
        else ""
    )
    bounded = apply_output_budget(tool_output, max_chars=max_chars)
    if bounded.status in {"pending", "pending_approval", "uncertain"}:
        summary = bounded.meta.summary or bounded.data or "操作尚未完成"
        return _short(str(summary), max_chars)
    if bounded.status == "failed":
        code = bounded.meta.quality_hints.get("error_code")
        suffix = f"（{code}）" if code else ""
        return f"工具未完成{suffix}：{_short(bounded.data or '执行失败', max_chars)}"
    if bounded.status == "cancelled":
        return "该操作已取消，未对工作区作出更改。"
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
        text = structured_text or f"{prefix}{bounded.data or bounded.meta.summary or '步骤已完成'}"
    if bounded.status == "partial":
        text += "\n结果不完整：可缩小范围后继续查询。"
    if bounded.meta.citations:
        text += f"\n来源数量：{min(len(bounded.meta.citations), 10)}"
    return text[:max_chars].rstrip() + ("…" if len(text) > max_chars else "")
