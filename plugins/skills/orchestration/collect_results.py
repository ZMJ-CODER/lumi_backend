"""Deterministic result collector exposed through the unified MCP gateway."""

from __future__ import annotations

import json

from app.agents.skills.base import Skill, SkillContext, SkillResult


class CollectResultsSkill(Skill):
    name = "collect_results"
    description = "汇集已完成原子任务的状态和简要结果，供最终汇报使用"
    category = "orchestration"
    environment = "server"
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "description": "已脱敏的任务结果摘要"}},
        "required": ["items"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        raw = params.get("items") or []
        if not isinstance(raw, list):
            return SkillResult(success=False, error="items 必须是列表", error_code="INVALID_ARGS")
        items = [item for item in raw[:500] if isinstance(item, dict)]
        payload = {
            "total": len(items),
            "completed": sum(item.get("status") == "completed" for item in items),
            "failed": sum(item.get("status") == "failed" for item in items),
            "cancelled": sum(item.get("status") == "cancelled" for item in items),
            "items": items,
        }
        return SkillResult(
            success=True,
            output=json.dumps(payload, ensure_ascii=False),
            metadata={"collected": len(items)},
        )
