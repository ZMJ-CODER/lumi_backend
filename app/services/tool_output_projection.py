"""工具结果投影：为模型提供可控摘要，避免把外部原文整段回灌。"""

from __future__ import annotations

from collections.abc import Mapping

from app.services.tool_output_pipeline import normalize_skill_result, render_for_model


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
    text = render_for_model(normalize_skill_result(result), max_chars=max_chars)

    signals = result.decision_signals()
    hints: list[str] = []
    if isinstance(signals.get("result_count"), int):
        hints.append(f"结果数={signals['result_count']}")
    if signals.get("truncated"):
        hints.append("结果已截断，请用更具体条件分批查询")
    if signals.get("refine_suggestion"):
        hints.append(f"细化建议={_short(signals['refine_suggestion'], 180)}")
    if hints:
        # 决策信号只作为结构化元数据供调用方使用，不把内部提示词注入模型正文。
        text += "\n[检索信号] " + "；".join(hints)
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
