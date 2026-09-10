"""ExecutionRouter：严格 8 步的任务路由（纯函数，无 IO）。

Step 1 安全策略 → Step 2 依赖校验 → Step 3 上下文就绪 → Step 4 副作用
→ Step 5 外部读取 → Step 6 模式选择 → Step 7 兜底 M2 → Step 8 工具级风控
（Step 8 由 ``safety_policy.SafetyGuard`` 在执行器调用）。

硬约束：``side_effects`` 非空时绝不返回 DIRECT_CHAT 或 M1_ATOMIC_READ。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from lumi_orch.task_assessment import (
    TaskProfile,
    has_side_effects,
    is_context_ready,
    needs_external_read,
)


class ExecutionMode(str, Enum):
    DIRECT_CHAT = "direct_chat"
    M1_ATOMIC_READ = "m1_atomic_read"
    M1_ATOMIC_ACTION = "m1_atomic_action"
    SEQUENTIAL_WORKFLOW = "sequential_workflow"
    DYNAMIC_AGENT = "dynamic_agent"


@dataclass(frozen=True, slots=True)
class RouteDecision:
    mode: ExecutionMode | None
    blocked: bool = False
    reason_code: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.blocked and self.mode is not None


def route(
    profile: TaskProfile,
    *,
    security_violated: bool = False,
    workspace_bound: bool = True,
    service_authorized: bool = True,
    estimated_steps: int | None = None,
) -> RouteDecision:
    """按 8 步顺序给出路由决策（纯函数，依赖项以布尔注入）。"""
    # === Step 1: 安全策略检查（注入/违禁词由宿主判定后传入）===
    if security_violated:
        return RouteDecision(None, blocked=True, reason_code="SECURITY_VIOLATION",
                             reason="输入被安全策略拦截")

    # === Step 2: 依赖校验（仅当画像确实依赖时）===
    if "WORKSPACE" in profile.info_sources or profile.execution_target == "DESKTOP":
        if not workspace_bound:
            return RouteDecision(None, blocked=True, reason_code="DEPENDENCY_MISSING_WORKSPACE",
                                 reason="请先绑定工作区或本地设备")
    if "PRIVATE_SERVICE" in profile.info_sources and not service_authorized:
        return RouteDecision(None, blocked=True, reason_code="DEPENDENCY_MISSING_SERVICE",
                             reason="缺少第三方服务授权")

    # === Step 3/4/5: 就绪性、副作用、外部读取 ===
    context_ready = is_context_ready(profile)
    side_effects = has_side_effects(profile)
    external_read = needs_external_read(profile)
    steps = estimated_steps or profile.estimated_steps or 0

    # === Step 6: 模式选择 ===
    mode: ExecutionMode | None = None

    # 6.1 M0：内容就绪 + 无副作用 + 无外部读取 + 纯模型计算
    if context_ready and not side_effects and not external_read and profile.execution_target == "NONE":
        mode = ExecutionMode.DIRECT_CHAT

    if mode is None and profile.complexity == "M1":
        # 6.2.2 先判副作用动作：有副作用且路径 KNOWN → 原子动作
        if side_effects and profile.path_determinism == "KNOWN":
            mode = ExecutionMode.M1_ATOMIC_ACTION
        # 6.2.1 只读外部读取：必须无副作用
        elif external_read and not side_effects:
            mode = ExecutionMode.M1_ATOMIC_READ

    if mode is None and (
        profile.complexity == "M2" or (profile.path_determinism == "KNOWN" and steps > 1)
    ):
        mode = ExecutionMode.SEQUENTIAL_WORKFLOW

    if mode is None and (profile.complexity == "M3" or profile.path_determinism == "UNKNOWN"):
        mode = ExecutionMode.DYNAMIC_AGENT

    # === Step 7: 兜底 M2（防止失控；副作用任务绝不落到只读/直答）===
    if mode is None:
        mode = ExecutionMode.SEQUENTIAL_WORKFLOW

    # 硬约束兜底：有副作用却算出只读/直答 → 修正为原子动作或编排。
    if side_effects and mode in {ExecutionMode.DIRECT_CHAT, ExecutionMode.M1_ATOMIC_READ}:
        mode = (
            ExecutionMode.M1_ATOMIC_ACTION
            if profile.path_determinism == "KNOWN"
            else ExecutionMode.SEQUENTIAL_WORKFLOW
        )

    return RouteDecision(mode)


__all__ = ["ExecutionMode", "RouteDecision", "route"]
