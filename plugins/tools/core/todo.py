"""本地待办基础工具；不依赖历史办公 Tool。"""

import json
from pathlib import Path

from app.agents.skills.base import SkillContext, Tool, ToolOutput


def _todo_file(user_id: str) -> Path:
    safe = "".join(char for char in str(user_id or "anon") if char.isalnum() or char in "-_") or "anon"
    return Path(__file__).resolve().parents[3] / "data" / "todos" / f"{safe}.json"


def _save(user_id: str, items: list[dict]) -> None:
    path = _todo_file(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _fmt(items: list[dict]) -> str:
    if not items:
        return "（暂无待办）"
    return "\n".join(
        f"{index}. [{'✓' if item.get('done') else '○'}] {item.get('content') or ''}"
        + (f"（截止：{item['due']}）" if item.get("due") else "")
        for index, item in enumerate(items, 1)
    )


class TodoWriteTool(Tool):
    name = "TodoWrite"
    description = "按当前用户维护任务清单；用于记录任务进度，不代替业务工作流。"
    category = "productivity"
    domain = "productivity"
    resource = "todo"
    environment = "server"
    write_op = True
    requires_confirmation = True
    parameters_schema = {
        "type": "object",
        "properties": {"todos": {"type": "array", "items": {"type": "object"}, "maxItems": 200}},
        "required": ["todos"],
    }
    use_when = ["用户要求创建或更新任务清单"]
    do_not_use_when = ["只是回答问题或执行一次性动作"]
    result_contract = "返回当前任务清单。"

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        if not context or not context.user_id:
            return ToolOutput(success=False, error="需要登录后使用", error_code="AUTH_REQUIRED", retryable=False)
        todos = params.get("todos")
        if not isinstance(todos, list):
            return ToolOutput(success=False, error="todos 必须是数组", error_code="INVALID_ARGS", retryable=False)
        normalized = []
        for item in todos:
            if not isinstance(item, dict) or not str(item.get("content") or "").strip():
                return ToolOutput(success=False, error="每个 todo 必须包含 content", error_code="INVALID_ARGS", retryable=False)
            normalized.append({"id": str(item.get("id") or ""), "content": str(item["content"]).strip(), "done": bool(item.get("done")), "due": item.get("due")})
        _save(context.user_id, normalized)
        return ToolOutput(success=True, data={"todos": normalized}, metadata={"summary": _fmt(normalized)})
