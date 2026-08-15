"""办公技能（office/日程待办）：todo_manager —— 个人日程/待办管理（本地 JSON 存储）."""

import json
import os
import time
import uuid
from pathlib import Path

from app.agents.skills.base import Skill, SkillContext, SkillResult

TODO_DIR = Path(__file__).resolve().parents[3] / "data" / "todos"


def _todo_file(user_id: str) -> Path:
    safe = "".join(c for c in str(user_id or "anon") if c.isalnum() or c in "-_") or "anon"
    return TODO_DIR / f"{safe}.json"


def _load(user_id: str) -> list[dict]:
    try:
        p = _todo_file(user_id)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return []


def _save(user_id: str, items: list[dict]) -> None:
    p = _todo_file(user_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _fmt(items: list[dict]) -> str:
    if not items:
        return "（暂无待办）"
    lines = []
    for i, it in enumerate(items, 1):
        mark = "✓" if it.get("done") else "○"
        due = f"（截止：{it.get('due') or '未设'}）" if it.get("due") else ""
        lines.append(f"{i}. [{mark}] {it.get('content') or ''} {due}")
    return "\n".join(lines)


class TodoManagerSkill(Skill):
    name = "todo_manager"
    description = (
        "个人日程/待办管理：新增、查看、完成、删除待办事项（按用户隔离，保存在本地）。"
        "动作 action=add/list/complete/delete"
    )
    category = "office"
    environment = "server"
    write_op = True
    scenes = ["office", "chat"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "add/list/complete/delete"},
            "content": {"type": "string", "description": "待办内容（add 必填）"},
            "due": {"type": "string", "description": "截止时间，如 明天18:00 / 2026-08-20（可选）"},
            "item_id": {"type": "string", "description": "待办 id（complete/delete 必填）"},
        },
        "required": ["action"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        action = str(params.get("action") or "").strip().lower()
        items = _load(context.user_id)
        if action == "add":
            content = str(params.get("content") or "").strip()
            if not content:
                return SkillResult(success=False, error="add 需要 content", error_code="INVALID_ARGS", retryable=False)
            items.append(
                {
                    "id": uuid.uuid4().hex[:8],
                    "content": content,
                    "due": str(params.get("due") or "").strip() or None,
                    "done": False,
                    "created_at": time.time(),
                }
            )
            _save(context.user_id, items)
            return SkillResult(success=True, output=f"已添加待办：{content}\n\n当前待办：\n{_fmt(items)}")
        if action == "list":
            return SkillResult(success=True, output=_fmt(items))
        if action in ("complete", "delete"):
            item_id = str(params.get("item_id") or "").strip()
            target = next((x for x in items if x.get("id") == item_id), None)
            if not target:
                return SkillResult(success=False, error="未找到该待办 id", error_code="INVALID_ARGS", retryable=False)
            if action == "complete":
                target["done"] = True
                msg = f"已完成：{target.get('content')}"
            else:
                items.remove(target)
                msg = f"已删除：{target.get('content')}"
            _save(context.user_id, items)
            return SkillResult(success=True, output=f"{msg}\n\n当前待办：\n{_fmt(items)}")
        return SkillResult(success=False, error="action 仅支持 add/list/complete/delete", error_code="INVALID_ARGS", retryable=False)
