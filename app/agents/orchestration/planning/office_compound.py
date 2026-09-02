"""小型、确定性的办公组合计划。"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass

from app.agents.orchestration.models import TaskNode


_QUOTED_TODO_ADD = re.compile(r"[\"“‘「](?P<content>[^\"”’」\n]{2,160})[\"”’」]\s*(?:加(?:入|进|到)|添加(?:到|进)?|放(?:入|进|到)?|记(?:入|到)?)(?:我(?:的)?|当前用户的?)?(?:待办|任务清单|提醒)", re.IGNORECASE)
_TODO_ADD = re.compile(r"(?:添加|新建|创建|加入|加进|加到|放入|放进|放到|记入).{0,20}(?:我的|当前用户的)?(?:待办|任务清单|提醒)", re.IGNORECASE)
_UNQUOTED_TODO_ADD = re.compile(r"(?:把|将)\s*(?P<content>[^，。；;\n]{2,80}?)\s*(?:加入|加进|加到|添加到|添加进|放进|放到|记入|记到)(?:我(?:的)?|当前用户的?)?(?:待办|任务清单|提醒)", re.IGNORECASE)
_TEXT_OUTPUT = re.compile(r"(?:整理|总结|概括|提炼|改写|润色|起草|撰写|生成|列出|输出|写(?:一|个|成|出))", re.IGNORECASE)
_CONFIRM_PREFIX = re.compile(r"(?:确认(?:前面|前两部分|以上|内容)?(?:没问题|无误|可以)?后[，,、]?(?:再|然后)?(?:把)?\s*)$")


@dataclass(frozen=True, slots=True)
class CompoundOfficePlan:
    """保留显式持久化操作的小型静态计划。"""

    nodes: list[TaskNode]
    plan_text: str


def is_explicit_todo_add_request(request: str) -> bool:
    text = str(request or "").strip()
    return bool(_QUOTED_TODO_ADD.search(text) or _UNQUOTED_TODO_ADD.search(text) or _TODO_ADD.search(text))


def _node_id(prefix: str) -> str:
    return f"{prefix}{int(time.time())}-{uuid.uuid4().hex[:6]}"


def _text_without_todo_clause(request: str, match: re.Match[str]) -> str:
    before = request[:match.start()]
    prefix = _CONFIRM_PREFIX.search(before)
    if prefix:
        before = before[:prefix.start()]
    before = re.sub(r"(?:帮我|请帮我|麻烦帮我)?\s*把\s*$", "", before)
    return (before + request[match.end():]).strip(" \t\r\n，,。；;")


def build_text_then_todo_plan(request: str) -> CompoundOfficePlan | None:
    """编译“生成文本 → 写入待办”；待办节点仍需执行期确认。"""
    text = str(request or "").strip()
    match = _QUOTED_TODO_ADD.search(text) or _UNQUOTED_TODO_ADD.search(text)
    if match is None:
        return None
    content = str(match.group("content") or "").strip()
    if not content:
        return None
    todo_id = _node_id("todo")
    todo_node = TaskNode(
        id=todo_id,
        name="加入待办",
        agent="atomic_step",
        params={"instruction": f"将待办“{content}”添加到当前用户的待办列表。", "preferred_tool": "todo_manager", "fallback_tools": [], "inputs": {"action": "add", "content": content}},
        approval=True,
        approval_note="将写入当前用户的待办列表，需确认后执行。",
        metadata={"routing": {"reason": "explicit_todo_write", "route_channel": "agent"}},
    )
    text_instruction = _text_without_todo_clause(text, match)
    if not text_instruction or not _TEXT_OUTPUT.search(text_instruction):
        return CompoundOfficePlan(nodes=[todo_node], plan_text="确认后将指定内容加入待办。")
    text_id = _node_id("text")
    text_node = TaskNode(
        id=text_id,
        name="整理并生成文本结果",
        agent="atomic_step",
        params={"instruction": text_instruction, "preferred_tool": "summarize_text", "fallback_tools": [], "inputs": {"instruction": text_instruction}},
        metadata={"routing": {"reason": "text_before_explicit_todo", "route_channel": "direct_llm"}},
    )
    todo_node.depends_on = [text_id]
    return CompoundOfficePlan(nodes=[text_node, todo_node], plan_text="先整理文本结果；待确认后再将指定事项加入待办。")


__all__ = ["CompoundOfficePlan", "build_text_then_todo_plan", "is_explicit_todo_add_request"]
