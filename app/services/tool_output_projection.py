"""工具结果投影：为模型提供可控摘要，避免把外部原文整段回灌。"""

from __future__ import annotations

from collections.abc import Mapping


MODEL_OUTPUT_MAX_CHARS = 2200
ITEM_SUMMARY_MAX_CHARS = 240


def _short(value: object, limit: int = ITEM_SUMMARY_MAX_CHARS) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def project_tool_output(result, *, max_chars: int = MODEL_OUTPUT_MAX_CHARS) -> str:
    """生成模型侧工具结果。

    外部网页、知识库等结果优先使用结构化 citations 的短摘要；没有结构化
    引用时才对普通工具输出做总长截断。完整结果仍由工具审计/持久化链路保留。
    """
    if not result.success:
        code = result.error_code or "EXEC_ERROR"
        guidance = {
            "INVALID_ARGS": "请核对必填参数和枚举值；不能确定时向用户澄清。",
            "FORBIDDEN": "该范围未获授权；不要尝试猜测或扩大参数范围。",
            "NEEDS_CONFIRMATION": "该操作等待用户确认；不要改用其他写工具绕过确认。",
            "TIMEOUT": "工具超时；可缩小范围后重试，或说明限制。",
        }.get(code, "请根据错误说明修正参数、换用更合适的已授权工具，或直接说明限制。")
        return f"工具未完成（{code}）：{result.error or '执行失败'}\n下一步：{guidance}"

    metadata = result.metadata if isinstance(result.metadata, Mapping) else {}
    citations = metadata.get("citations")
    lines: list[str] = []
    if isinstance(citations, list) and citations:
        lines.append("[工具摘要]")
        lines.append(f"共返回 {len(citations)} 条来源。请提取相关事实并归纳，不要逐字复制原文。")
        for index, citation in enumerate(citations[:10], 1):
            if not isinstance(citation, Mapping):
                continue
            title = _short(citation.get("title") or "未命名来源", 140)
            source = _short(citation.get("source") or citation.get("url") or "", 300)
            summary = _short(citation.get("content") or citation.get("snippet") or "")
            lines.append(f"{index}. {title}")
            if source:
                lines.append(f"   来源：{source}")
            if summary:
                lines.append(f"   摘要：{summary}")
        text = "\n".join(lines)
    else:
        text = _short(result.output or "步骤已完成", max_chars)

    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"

    signals = result.decision_signals()
    hints: list[str] = []
    if isinstance(signals.get("result_count"), int):
        hints.append(f"结果数={signals['result_count']}")
    if signals.get("truncated"):
        hints.append("结果已截断，请用更具体条件分批查询")
    if signals.get("refine_suggestion"):
        hints.append(f"细化建议={_short(signals['refine_suggestion'], 180)}")
    if hints:
        text += "\n[决策信号] " + "；".join(hints)
    return text


def project_citations(citations: object, *, max_items: int = 10, max_chars: int = ITEM_SUMMARY_MAX_CHARS) -> list[dict]:
    """限制发送到客户端的引用摘要长度，不改变服务端审计原文。"""
    if not isinstance(citations, list):
        return []
    projected: list[dict] = []
    for item in citations[:max_items]:
        if not isinstance(item, Mapping):
            continue
        entry = dict(item)
        if "content" in entry:
            entry["content"] = _short(entry["content"], max_chars)
        projected.append(entry)
    return projected
