"""规则引擎：硬逻辑不交给 LLM（必填字段 / 阈值 / 权限等）.

在 Agent 执行技能前做动态校验（WorkerAgent.run_skill 接入）。
规则按 (agent, skill) 组织；返回违规原因列表，非空则拦截执行。
"""

from __future__ import annotations

from typing import Any, Callable


def _require(params: dict, key: str, label: str) -> str | None:
    return None if str(params.get(key) or "").strip() else f"{label}不能为空"


def _require_list(params: dict, key: str, label: str) -> str | None:
    return None if params.get(key) else f"{label}不能为空"


# 规则表：(agent_name, skill_name) -> [(params, user_id, context) -> 违规原因或 None]
RULES: dict[tuple[str, str], list[Callable[[dict, str | None, Any], str | None]]] = {
    ("office_todo", "todo_manager"): [
        lambda p, u, c: (
            None
            if p.get("action") != "add" or str(p.get("content") or "").strip()
            else "add 操作缺少待办内容"
        ),
    ],
    ("office_doc", "office_doc_read"): [
        lambda p, u, c: _require(p, "doc_id", "doc_id"),
    ],
    ("office_doc", "office_doc_edit"): [
        lambda p, u, c: _require(p, "doc_id", "doc_id"),
        lambda p, u, c: _require(p, "instruction", "编辑指令"),
    ],
    ("office_doc", "office_doc_analyze"): [
        lambda p, u, c: _require(p, "doc_id", "doc_id"),
        lambda p, u, c: _require(p, "instruction", "分析指令"),
    ],
    ("office_text", "compose_email"): [
        lambda p, u, c: (
            None
            if (p.get("instruction") or p.get("key_points"))
            else "邮件内容为空（需要 instruction 或 key_points）"
        ),
    ],
    ("office_research", "competitor_analysis"): [
        lambda p, u, c: _require(p, "product", "目标产品"),
    ],
    ("office_research", "document_qa"): [
        lambda p, u, c: _require(p, "question", "问题"),
    ],
}


def check_rules(
    agent_name: str,
    skill_name: str,
    params: dict,
    user_id: str | None = None,
    context: Any = None,
) -> list[str]:
    """执行技能前规则校验；返回违规原因列表（空 = 通过）."""
    violations: list[str] = []
    for fn in RULES.get((agent_name, skill_name), []):
        try:
            msg = fn(params or {}, user_id, context)
            if msg:
                violations.append(str(msg))
        except Exception:  # noqa: BLE001
            continue
    return violations
