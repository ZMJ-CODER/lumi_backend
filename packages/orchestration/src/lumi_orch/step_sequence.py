"""人可见“执行步骤序列”的纯模型与 PlanPatch 应用逻辑。

与 DAG 扩图 PlanPatch（lumi_orch/scheduling，面向 NodeSlot）不同，本模块
面向前端可见的步骤序列（step_confirm/auto_routine），语义：
  - 已完成（running/completed/waiting_approval）步骤不可改；
  - 只能插入/替换未执行步骤；
  - plan_revision 单调 +1（base_revision 必须等于当前 revision，否则拒绝过期补丁）；
  - step_confirm：计划变化需要展示给用户；
  - auto_routine：普通调整自动接受（是否高危由 ApprovalPolicyEngine 决定）。

纯逻辑层：输入输出均为 JSON-safe dict，不含持久化/网络副作用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

STEP_STATUSES = {"pending", "running", "completed", "failed", "waiting_approval"}
_IMMUTABLE_STATUSES = {"running", "completed", "waiting_approval"}


@dataclass(slots=True)
class StepPatch:
    """第 9 点 PlanPatch（步骤层）。"""

    base_revision: int
    reason: str
    insert_after: str | None = None
    steps: list[dict] = field(default_factory=list)
    patch_id: str = ""

    def to_dict(self) -> dict:
        return {
            "base_revision": self.base_revision,
            "reason": str(self.reason or ""),
            "insert_after": self.insert_after,
            "steps": [dict(s) for s in self.steps if isinstance(s, dict)],
            "patch_id": str(self.patch_id or ""),
        }


def normalize_step(raw: dict) -> dict:
    step = dict(raw or {})
    step["id"] = str(step.get("id") or "").strip()
    step["title"] = str(step.get("title") or step.get("name") or "步骤")[:200]
    step["description"] = str(step.get("description") or step.get("desc") or "")[:1000]
    step["domain"] = str(step.get("domain") or "workspace")[:40]
    status = str(step.get("status") or "pending").strip().lower()
    if status not in STEP_STATUSES:
        status = "pending"
    step["status"] = status
    step.setdefault("result_ref", None)
    return step


def _step_ids(steps: list[dict]) -> list[str]:
    return [str(s.get("id") or "") for s in steps if isinstance(s, dict) and str(s.get("id") or "").strip()]


def _has_duplicates(ids: list[str]) -> bool:
    return len(ids) != len(set(ids))


def apply_step_patch(
    state: dict[str, Any],
    patch: StepPatch,
    *,
    execution_mode: str = "step_confirm",
) -> dict[str, Any]:
    """应用步骤补丁；任何不变量违反都以 error 返回（不抛异常）。

    返回 {"ok": bool, "error": str|None, "steps": [...], "plan_revision": int,
          "should_display": bool, "changed": bool}
    """
    steps = [normalize_step(s) for s in (state.get("steps") or []) if isinstance(s, dict)]
    revision = int(state.get("plan_revision") or 1)
    existing_ids = _step_ids(steps)
    executed = {sid for sid in existing_ids if _step_status(steps, sid) in _IMMUTABLE_STATUSES}

    def fail(reason: str) -> dict:
        return {"ok": False, "error": reason, "steps": steps,
                "plan_revision": revision, "should_display": False, "changed": False}

    if patch.base_revision != revision:
        return fail("计划版本已变化（base_revision 过期），拒绝应用补丁")
    new_steps = [normalize_step(s) for s in patch.steps if isinstance(s, dict)]
    new_ids = _step_ids(new_steps)
    if not new_ids:
        return fail("补丁不包含任何步骤")
    if not new_steps or any(not s.get("id") for s in new_steps):
        return fail("补丁步骤缺少 id")
    if _has_duplicates([*existing_ids, *new_ids]):
        return fail("补丁步骤 id 与现有计划冲突")
    if executed & set(new_ids):
        return fail("不允许修改已执行步骤")

    insert_after = str(patch.insert_after or "").strip() or None
    if insert_after is not None and insert_after not in existing_ids:
        return fail(f"插入锚点不存在: {insert_after}")
    if insert_after is not None and _step_status(steps, insert_after) in {"running", "waiting_approval"}:
        return fail("不能在正在执行或等待审批的步骤之后插入（避免重排进行中的流程）")

    if insert_after is None:
        merged = [*steps, *new_steps]
    else:
        index = existing_ids.index(insert_after) + 1
        merged = [*steps[:index], *new_steps, *steps[index:]]
    return {
        "ok": True,
        "error": None,
        "steps": merged,
        "plan_revision": revision + 1,
        "should_display": str(execution_mode or "").strip() == "step_confirm",
        "changed": True,
        "reason": str(patch.reason or "")[:500],
    }


def _step_status(steps: list[dict], step_id: str) -> str:
    for step in steps:
        if str(step.get("id") or "") == step_id:
            return str(step.get("status") or "pending")
    return "pending"


def step_execution_context(
    *,
    user_request: str,
    step: dict,
    prior_results: list[dict] | None = None,
    workspace_summary: str = "",
    skill_prompt: str = "",
    allowed_tools: list[str] | None = None,
) -> dict[str, Any]:
    """第 5 点：为单个步骤组装“精简执行上下文”。

    只包含 原始目标 / 当前步骤 / 必要前置结果 / 工作区摘要 / 当前技能提示 /
    本轮允许工具，不重放完整对话与完整工具历史。
    """
    prior = prior_results or []
    return {
        "user_request": str(user_request or "")[:4000],
        "step": {
            "id": str(step.get("id") or ""),
            "title": str(step.get("title") or ""),
            "description": str(step.get("description") or ""),
            "domain": str(step.get("domain") or ""),
        },
        "prior_results": [dict(item) for item in prior[-6:] if isinstance(item, dict)],
        "workspace_summary": str(workspace_summary or "")[:4000],
        "skill_prompt": str(skill_prompt or "")[:4000],
        "allowed_tools": list(allowed_tools or []),
    }
