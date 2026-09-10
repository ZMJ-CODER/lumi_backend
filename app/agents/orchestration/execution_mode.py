"""办公任务执行模式（direct / step_confirm / auto_routine）与审批模式映射。

方案要点（与“帮我确认”语义对应）：
  - 关闭“帮我确认”（approval_mode=manual_commit）→ execution_mode=step_confirm：
    先展示计划并置 waiting_run；每完成一步等待用户“运行下一步”；
    用户点击按钮即是对当前普通步骤的授权，普通提交不再重复弹确认。
  - 开启“帮我确认”（approval_mode=auto_routine）→ execution_mode=auto_routine：
    计划仍返回展示；普通步骤自动连续执行；每步独立持久化、独立 SSE 请求。
  - 纯问答/轻量任务 → direct（不产生执行计划按钮）。

本模块只提供纯判定/映射，不触碰业务关键词：是否进入执行模式由任务画像
（goal / complexity / safety_level / required_sources / workspace 可用性）决定，
而不是固定业务类别。
"""

from __future__ import annotations

from typing import Any

from app.services.workspace_context import (
    APPROVAL_MODE_AUTO,
    APPROVAL_MODE_CONFIRM,
)

EXECUTION_DIRECT = "direct"
EXECUTION_STEP_CONFIRM = "step_confirm"
EXECUTION_AUTO_ROUTINE = "auto_routine"
EXECUTION_MODES = {EXECUTION_DIRECT, EXECUTION_STEP_CONFIRM, EXECUTION_AUTO_ROUTINE}

# /chat/stream execution_preference 取值。
PREFERENCE_USE_WORKSPACE_POLICY = "use_workspace_policy"
PREFERENCE_STEP_CONFIRM = EXECUTION_STEP_CONFIRM
PREFERENCE_AUTO_ROUTINE = EXECUTION_AUTO_ROUTINE

# 计划式 Job 的规范执行状态（第 5 点；底层仍复用现有 JobStatus 最近邻落盘，
# canonical 写入 routing["execution_state"] 保持前端契约稳定）。
EXECUTION_JOB_STATES = (
    "planning",
    "waiting_run",
    "running_step",
    "waiting_next",
    "waiting_approval",
    "completed",
    "failed",
    "cancelled",
)

# canonical → 现有 JobStatus 最近邻（供持久化与状态机过渡使用）。
# 前后端联调 SSE 事件名（第 10 点契约；值固定为协议名）。
SSE_EVENT_PLAN_DELTA = "plan_delta"
SSE_EVENT_PLAN_READY = "plan_ready"
SSE_EVENT_DONE = "done"
SSE_EVENT_STEP_STARTED = "step_started"
SSE_EVENT_PROCESS = "process"
SSE_EVENT_TOOL_STARTED = "tool_started"
SSE_EVENT_TOOL_COMPLETED = "tool_completed"
SSE_EVENT_STEP_COMPLETED = "step_completed"
SSE_EVENT_WAITING_NEXT = "waiting_next"
SSE_EVENT_WAITING_APPROVAL = "waiting_approval"
SSE_EVENT_TASK_COMPLETED = "task_completed"
SSE_EVENT_TASK_FAILED = "task_failed"
SSE_EVENTS = frozenset({
    SSE_EVENT_PLAN_DELTA,
    SSE_EVENT_PLAN_READY,
    SSE_EVENT_DONE,
    SSE_EVENT_STEP_STARTED,
    SSE_EVENT_PROCESS,
    SSE_EVENT_TOOL_STARTED,
    SSE_EVENT_TOOL_COMPLETED,
    SSE_EVENT_STEP_COMPLETED,
    SSE_EVENT_WAITING_NEXT,
    SSE_EVENT_WAITING_APPROVAL,
    SSE_EVENT_TASK_COMPLETED,
    SSE_EVENT_TASK_FAILED,
})

_EXISTING_STATUS_BY_STATE = {
    "planning": "pending",
    "waiting_run": "pending",
    "running_step": "running",
    "waiting_next": "pending",
    "waiting_approval": "waiting_approval",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}

_HIGH_RISK_SAFETY = {"RISKY_WRITE", "CRITICAL"}


def resolve_execution_mode(
    *,
    preference: str = PREFERENCE_USE_WORKSPACE_POLICY,
    approval_mode: str = APPROVAL_MODE_CONFIRM,
) -> str:
    """根据前端 preference + 工作区 approval_mode 解析执行模式。

    - use_workspace_policy：由 approval_mode 决定
      （manual_commit→step_confirm，auto_routine→auto_routine）；
    - step_confirm / auto_routine：显式覆盖（direct 不可由 preference 指定，
      只能由任务画像判定为轻量直答）。
    """
    pref = str(preference or "").strip()
    if pref == EXECUTION_STEP_CONFIRM:
        return EXECUTION_STEP_CONFIRM
    if pref == EXECUTION_AUTO_ROUTINE:
        return EXECUTION_AUTO_ROUTINE
    if pref == EXECUTION_DIRECT:
        return EXECUTION_DIRECT
    # use_workspace_policy（含空/未知值回退）：
    mode = str(approval_mode or "").strip()
    if mode == APPROVAL_MODE_AUTO:
        return EXECUTION_AUTO_ROUTINE
    return EXECUTION_STEP_CONFIRM


def execution_required(
    profile: dict[str, Any] | None,
    *,
    workspace_available: bool = False,
    has_attachments: bool = False,
) -> bool:
    """任务画像 → 是否需要进入计划式执行（mode4 的纯函数判定）。

    以下任一命中即需要执行计划：外部能力/多步依赖/修改状态/需要后续决策/
    运行验证或交付；纯“读取已提供内容并回答”的轻量任务返回 False。
    """
    if not isinstance(profile, dict):
        # 无画像的办公请求默认保守进入 step_confirm，除非工作区不可用且无附件。
        return bool(workspace_available or has_attachments)
    goal = str(profile.get("goal") or "").upper()
    complexity = str(profile.get("complexity") or "").upper()
    safety = str(profile.get("safety_level") or "").upper()
    sources = {str(item).upper() for item in (profile.get("required_sources") or [])}

    if complexity in {"SEQUENTIAL", "DYNAMIC"}:
        return True
    if goal in {"EXECUTE", "INTERACT"}:
        return True
    if safety in _HIGH_RISK_SAFETY:
        return True
    if not sources.issubset({"USER_INPUT", "ATTACHED_FILE"}):
        # 需要外部能力 / 本地工作区状态 / 系统状态 / 公开网络等 → 执行型。
        return True
    if has_attachments or workspace_available:
        # 默认需要一个可执行计划来承载读取后回答；direct 只给纯文本轻量场景。
        return True
    return False


def plan_first_eligible(
    *,
    scene: str = "office",
    requires_orchestration: bool = True,
    execution_preference: str = PREFERENCE_USE_WORKSPACE_POLICY,
    approval_mode: str = "",
    plan_first_global: bool = False,
    has_workspace_grant: bool = False,
) -> bool:
    """计划优先（首轮只出计划并置 waiting_run）的启用判定（纯函数）。

    判定顺序（与前端/产品语义一致）：
      - 非办公或轻量直答（requires_orchestration=False）→ 始终不启用；
      - 显式 execution_preference=step_confirm → 始终计划优先；
      - 显式 auto_routine / direct → 不启用（展示计划后自动推进/直答）；
      - 未显式指定（None/''/use_workspace_policy）：
          仅当全局开关 EXECUTION_PLAN_FIRST 开启、且工作区授权快照存在、
          且 approval_mode=manual_commit（关闭“帮我确认”）时才计划优先；
          无授权快照时默认不自动启用（除非前端显式传 step_confirm）。
    """
    if str(scene or "") != "office" or not requires_orchestration:
        return False
    pref = str(execution_preference or "").strip()
    if pref == EXECUTION_STEP_CONFIRM:
        return True
    if pref in {EXECUTION_AUTO_ROUTINE, EXECUTION_DIRECT}:
        return False
    # use_workspace_policy（含空/未知值回退）
    if not plan_first_global:
        return False
    if not has_workspace_grant:
        return False
    return str(approval_mode or "").strip() == APPROVAL_MODE_CONFIRM


def initial_execution_state(
    execution_mode: str,
    *,
    plan_first_enabled: bool = False,
) -> str:
    """计划式任务的初始规范状态。

    step_confirm（且开启计划优先）→ waiting_run（首轮只生成计划，不自动派发）；
    其余 → planning（沿用现行先规划后执行的兼容状态，由执行循环推进）。
    """
    if plan_first_enabled and execution_mode == EXECUTION_STEP_CONFIRM:
        return "waiting_run"
    return "planning"


def canonical_state(status: str) -> str | None:
    value = str(status or "").strip()
    return value if value in EXECUTION_JOB_STATES else None


def existing_status_for_state(state: str) -> str | None:
    return _EXISTING_STATUS_BY_STATE.get(str(state or "").strip())
