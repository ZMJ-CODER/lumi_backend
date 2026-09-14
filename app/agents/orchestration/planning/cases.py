"""成功任务案例库：保存历史成功任务，规划时做 Few-Shot 参考（提高规划质量）.

存储：Redis list（cap 100）。相似度：请求文本的中文 2-gram + 英文词重叠打分。
"""

from __future__ import annotations

import json

from app.core.redis import get_redis

_CASE_KEY = "agent_success_cases"
_CASE_CAP = 100


def _grams(text: str) -> set[str]:
    """中文 2-gram + 英文词."""
    grams = set()
    t = str(text or "")
    for i in range(len(t) - 1):
        if "\u4e00" <= t[i] <= "\u9fff" or "\u4e00" <= t[i + 1] <= "\u9fff":
            grams.add(t[i : i + 2])
    for w in t.replace(",", " ").replace("，", " ").split():
        if w.isalnum() and len(w) >= 2:
            grams.add(w.lower())
    return grams


async def save_success_case(user_id: str, request: str, nodes: list[dict]) -> None:
    """任务完成后保存案例（失败静默，不阻塞主流程）."""
    try:
        case = {
            "user_id": str(user_id or ""),
            "request": str(request or "")[:500],
            "agents": [str(n.get("agent") or "") for n in (nodes or [])],
            "node_summary": " -> ".join(str(n.get("name") or n.get("agent") or "") for n in (nodes or [])[:6]),
        }
        r = get_redis()
        await r.lpush(_CASE_KEY, json.dumps(case, ensure_ascii=False))
        await r.ltrim(_CASE_KEY, 0, _CASE_CAP - 1)
    except Exception:  # noqa: BLE001
        pass


async def get_similar_cases(request: str, limit: int = 3) -> list[dict]:
    """按请求相似度返回历史成功案例（top limit 条）."""
    try:
        r = get_redis()
        raws = await r.lrange(_CASE_KEY, 0, _CASE_CAP - 1)
        qg = _grams(request)
        scored = []
        for raw in raws:
            try:
                case = json.loads(raw)
            except (TypeError, ValueError):
                continue
            cg = _grams(case.get("request", ""))
            overlap = len(qg & cg)
            if overlap > 0:
                scored.append((overlap, case))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored[:limit]]
    except Exception:  # noqa: BLE001
        return []


def format_cases(cases: list[dict]) -> str:
    """Few-Shot 文本（拼接进规划提示词）."""
    if not cases:
        return ""
    lines = ["参考历史成功任务的处理方式（结构可借鉴，参数按本次需求调整）："]
    for i, c in enumerate(cases, 1):
        lines.append(f"{i}. 需求：{c.get('request', '')[:120]} | 处理：{c.get('node_summary', '')}")
    return "\n".join(lines)
