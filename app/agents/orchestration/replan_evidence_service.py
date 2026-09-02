"""为重规划提示词构建有界、脱敏的执行证据。"""

from __future__ import annotations

import json

from app.agents.orchestration.models import TaskStatus


class ReplanEvidenceService:
    @staticmethod
    def _public_text(value: object, limit: int) -> str:
        text = str(value or "").strip()
        try:
            from app.core.agent_security import redact_server_text

            text = redact_server_text(text)
        except Exception:  # noqa: BLE001
            pass
        return text[:limit]

    async def logical_plan_context(
        self, *, user_id: str, plan: dict, prior_summaries: str
    ) -> tuple[list[dict], str]:
        """Return failed evidence and a bounded planner continuation prompt."""
        from app.agents.orchestration.execution.lineage import resolve_result_ref

        completed: list[dict] = []
        failed: list[dict] = []
        records = plan.get("nodes") or {}
        for logical_id in list(plan.get("order") or []):
            record = records.get(logical_id)
            if not isinstance(record, dict):
                continue
            raw_node = record.get("node") or {}
            status = str(record.get("status") or "pending")
            if status == TaskStatus.COMPLETED.value:
                result = await resolve_result_ref(user_id, record.get("result_ref"))
                completed.append(
                    {
                        "step": self._public_text(raw_node.get("name") or logical_id, 120),
                        "result": self._public_text(
                            (result or {}).get("content") or (result or {}).get("output"),
                            900,
                        ),
                    }
                )
            elif status in {
                TaskStatus.FAILED.value,
                TaskStatus.ESCALATED.value,
                TaskStatus.SKIPPED.value,
            }:
                failed.append(
                    {
                        "step": self._public_text(raw_node.get("name") or logical_id, 120),
                        "method": self._public_text(
                            (raw_node.get("params") or {}).get("preferred_tool")
                            or raw_node.get("agent"),
                            120,
                        ),
                        "error_code": self._public_text(record.get("error_code"), 80),
                        "error": self._public_text(record.get("error"), 500),
                    }
                )
        payload = {
            "instruction": (
                "这是同一任务的计划演进。保留已完成产物，只规划尚未完成目标；"
                "不要重复已成功步骤；失败方法不得原样重试，应更换工具、参数或实现原理。"
            ),
            "completed": completed[-20:],
            "failed": failed[-10:],
        }
        context = (prior_summaries or "").strip()
        context += "\n\n[当前任务执行反馈]\n" + json.dumps(
            payload, ensure_ascii=False, default=str
        )
        return failed, context
