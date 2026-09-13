"""ExecutionRouter：严格 8 步的任务路由（纯函数，无 IO）。

Step 1 安全策略 → Step 2 依赖校验 → Step 3 上下文就绪 → Step 4 副作用
→ Step 5 外部读取 → Step 6 模式选择 → Step 7 兜底 M2 → Step 8 工具级风控
（Step 8 由 ``safety_policy.SafetyGuard`` 在执行器调用）。

**方案 4 §2.2 的三条硬约束**（不可绕过，且与置信度无关）：

1. ``action_intents`` 非空 → **禁止** ``DIRECT_CHAT``、**禁止** ``M1_ATOMIC_READ``
   （旧词表判"只读" + 新画像判"写入"时，新画像赢：必须进入受控编排并注入写工具）；
2. ``DELETE`` / ``EXECUTE`` / ``PUBLISH`` → **必须**审批或安全阻断，不因分类器
   高置信度自动执行（``approval_required=True``，由宿主转成审批挂起或阻断）；
3. 目标未知（``target_clarity == UNKNOWN`` 且有动作意图）→ **澄清**，不猜路径。

路由只做"模式选择"：不解析自然语言、不做关键词判定——画像（``TaskProfile``）是
唯一意图事实源。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from lumi_orch.task_assessment import (
    HIGH_RISK_SIDE_EFFECTS,
    READ_ONLY_ACTIONS,
    TaskProfile,
    effective_action_intents,
    has_side_effects,
    is_context_ready,
    needs_external_read,
    requires_approval,
)


def read_only_actions(actions: tuple[str, ...]) -> bool:
    """动作意图是否全是只读（空元组不算"只读"，那是"没有动作"）。"""
    return bool(actions) and all(str(item) in READ_ONLY_ACTIONS for item in actions)


class ExecutionMode(str, Enum):
    DIRECT_CHAT = "direct_chat"
    M1_ATOMIC_READ = "m1_atomic_read"
    M1_ATOMIC_ACTION = "m1_atomic_action"
    SEQUENTIAL_WORKFLOW = "sequential_workflow"
    DYNAMIC_AGENT = "dynamic_agent"


#: 路由原因码（稳定枚举；自由文本不得用于业务判断）。
REASON_SECURITY = "SECURITY_VIOLATION"
REASON_WORKSPACE_MISSING = "DEPENDENCY_MISSING_WORKSPACE"
REASON_SERVICE_MISSING = "DEPENDENCY_MISSING_SERVICE"
REASON_ACTIONS_PRESENT = "ACTION_INTENTS_REQUIRE_ORCHESTRATION"
REASON_HIGH_RISK_APPROVAL = "HIGH_RISK_REQUIRES_APPROVAL"
REASON_TARGET_UNKNOWN = "TARGET_CLARITY_UNKNOWN"
REASON_DIRECT = "GENERATE_ONLY_DIRECT_CHAT"
REASON_ATOMIC_READ = "SINGLE_READ_ONLY"
REASON_ATOMIC_ACTION = "SINGLE_WRITE_KNOWN_TARGET"
REASON_SEQUENTIAL = "MULTI_STEP_OR_DEPENDENCY"
REASON_DYNAMIC = "RUNTIME_DECISION"
REASON_FALLBACK = "FALLBACK_WORKFLOW"


@dataclass(frozen=True, slots=True)
class RouteDecision:
    mode: ExecutionMode | None
    blocked: bool = False
    reason_code: str = ""
    reason: str = ""
    #: 需要人工审批（宿主转成审批挂起；与 ``blocked`` 互斥，审批不是失败）。
    approval_required: bool = False
    #: 目标未知：进入澄清（不是失败，也不是执行）。
    needs_clarification: bool = False
    #: 本次决策采用的动作意图（审计/工具窗口注入用；来自画像，不重新解析文本）。
    action_intents: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.blocked and self.mode is not None

    @property
    def suspended(self) -> bool:
        """需要人工介入（审批或澄清）——两者都不允许"继续自动执行"。"""
        return bool(self.approval_required or self.needs_clarification)


def route(
    profile: TaskProfile,
    *,
    security_violated: bool = False,
    workspace_bound: bool = True,
    service_authorized: bool = True,
    estimated_steps: int | None = None,
    approval_required: bool | None = None,
) -> RouteDecision:
    """按 8 步顺序给出路由决策（纯函数，依赖项以布尔注入）。

    :param approval_required: 显式覆盖（宿主已知的策略结论）；缺省由画像推导
        （``requires_approval``）。
    """
    actions = effective_action_intents(profile)
    # === Step 1: 安全策略检查（注入/违禁词由宿主判定后传入）===
    if security_violated:
        return RouteDecision(None, blocked=True, reason_code=REASON_SECURITY,
                             reason="输入被安全策略拦截", action_intents=actions)

    # === Step 2: 依赖校验（仅当画像确实依赖时）===
    if "WORKSPACE" in profile.info_sources or profile.execution_target == "DESKTOP":
        if not workspace_bound:
            return RouteDecision(None, blocked=True, reason_code=REASON_WORKSPACE_MISSING,
                                 reason="请先绑定工作区或本地设备", action_intents=actions)
    if "PRIVATE_SERVICE" in profile.info_sources and not service_authorized:
        return RouteDecision(None, blocked=True, reason_code=REASON_SERVICE_MISSING,
                             reason="缺少第三方服务授权", action_intents=actions)

    # === Step 3/4/5: 就绪性、副作用、外部读取 ===
    context_ready = is_context_ready(profile)
    side_effects = has_side_effects(profile)
    external_read = needs_external_read(profile)
    steps = estimated_steps or profile.estimated_steps or 0
    approval = requires_approval(profile) if approval_required is None else bool(approval_required)

    # === §2.2 硬约束③：目标未知且有动作意图 → 澄清，不猜路径 ===
    # （澄清优先于模式选择：模式本身没错，是信息不够。）
    if actions and str(profile.target_clarity) == "UNKNOWN":
        return RouteDecision(
            None,
            reason_code=REASON_TARGET_UNKNOWN,
            reason="目标不明确，需要澄清后再执行",
            needs_clarification=True,
            action_intents=actions,
        )

    # === Step 6: 模式选择 ===
    mode: ExecutionMode | None = None

    # 6.1 M0：内容就绪 + 无副作用 + 无外部读取 + 纯模型计算
    # §2.2 硬约束①：动作意图非空时**不允许**直聊（旧词表判只读也要让位给新画像）。
    if (
        context_ready
        and not side_effects
        and not external_read
        and not actions
        and profile.execution_target == "NONE"
    ):
        mode = ExecutionMode.DIRECT_CHAT

    if mode is None and profile.complexity == "M1":
        # 6.2.2 先判副作用动作：有副作用且路径 KNOWN → 原子动作
        if side_effects and profile.path_determinism == "KNOWN":
            mode = ExecutionMode.M1_ATOMIC_ACTION
        # 6.2.1 单次明确读取：**只读动作意图**（READ/SEARCH）或外部读取，
        # 且没有副作用 → 原子读。注意这里不是"intents 非空就禁原子读"：
        # 禁的是"有写意图却走只读"，纯读意图本来就是原子读的正例。
        elif not side_effects and external_read and read_only_actions(actions):
            mode = ExecutionMode.M1_ATOMIC_READ
        elif not side_effects and actions:
            # 有**非只读**动作意图但没有旧词表的副作用信号：进入受控编排，
            # 由 Tool Window 按画像注入正确工具，绝不放回直聊（§2.2 硬约束①）。
            mode = ExecutionMode.M1_ATOMIC_ACTION

    if mode is None and (
        profile.complexity == "M2" or (profile.path_determinism == "KNOWN" and steps > 1)
    ):
        mode = ExecutionMode.SEQUENTIAL_WORKFLOW

    if mode is None and (profile.complexity == "M3" or profile.path_determinism == "UNKNOWN"):
        mode = ExecutionMode.DYNAMIC_AGENT

    # === Step 7: 兜底 M2（防止失控；副作用任务绝不落到只读/直答）===
    if mode is None:
        mode = ExecutionMode.SEQUENTIAL_WORKFLOW

    # 硬约束兜底①：有副作用、或**非只读**动作意图，却算出只读/直答 → 修正为
    # 原子动作或编排。纯只读意图（READ/SEARCH）本来就是原子读的正例：
    # 硬约束要拦的是"旧词表判只读、新画像判写入"的冲突，不是把所有读操作也拖进编排。
    needs_controlled = bool(side_effects) or bool(actions) and not read_only_actions(actions)
    if needs_controlled and mode in {ExecutionMode.DIRECT_CHAT, ExecutionMode.M1_ATOMIC_READ}:
        mode = (
            ExecutionMode.M1_ATOMIC_ACTION
            if profile.path_determinism == "KNOWN"
            else ExecutionMode.SEQUENTIAL_WORKFLOW
        )

    reason_code = _reason_code(
        mode,
        actions=actions,
        side_effects=side_effects,
        profile=profile,
        approval=approval,
        steps=steps,
    )
    # === §2.2 硬约束②：DELETE/EXECUTE/PUBLISH（或画像已标高风险）必须审批 ===
    if approval:
        # 高风险审批的原因码优先：``approval_required`` 是本次决策最重要的语义，
        # 不能被"原子动作/编排"这类模式描述盖掉（前端与验收按原因码分派审批卡）。
        return RouteDecision(
            mode,
            reason_code=REASON_HIGH_RISK_APPROVAL if _high_risk(profile) else reason_code,
            reason="高风险操作需要人工审批",
            approval_required=True,
            action_intents=actions,
        )
    return RouteDecision(mode, reason_code=reason_code, action_intents=actions)


def _high_risk(profile: TaskProfile) -> bool:
    """是否高风险动作：画像标了高风险，或动作意图/副作用里含 DELETE/EXECUTE/PUBLISH。

    **必须同时看动作意图**：新画像可以只给 ``action_intents=["DELETE"]`` 而不带旧
    ``side_effects``，只看副作用会让"新画像判删除"落到普通原子动作、绕过审批。
    """
    if profile.risk_level in {"REQUIRES_APPROVAL", "HIGH_RISK"}:
        return True
    if set(profile.side_effects or []) & HIGH_RISK_SIDE_EFFECTS:
        return True
    return bool(set(effective_action_intents(profile)) & HIGH_RISK_SIDE_EFFECTS)


def _reason_code(
    mode: ExecutionMode,
    *,
    actions: tuple[str, ...],
    side_effects: bool,
    profile: TaskProfile,
    approval: bool,
    steps: int,
) -> str:
    if mode is ExecutionMode.DIRECT_CHAT:
        return REASON_DIRECT
    if mode is ExecutionMode.M1_ATOMIC_READ:
        return REASON_ATOMIC_READ
    if mode is ExecutionMode.M1_ATOMIC_ACTION:
        # 动作意图驱动的原子动作（可能没有旧词表的副作用信号）——这是"旧读新写"
        # 修正后的落点，原因码必须能与旧路径区分开。
        if actions and not side_effects:
            return REASON_ACTIONS_PRESENT
        return REASON_ATOMIC_ACTION
    if mode is ExecutionMode.DYNAMIC_AGENT:
        return REASON_DYNAMIC
    if approval:
        return REASON_HIGH_RISK_APPROVAL
    if profile.complexity == "M2" or steps > 1 or bool(profile.has_dependency):
        return REASON_SEQUENTIAL
    return REASON_FALLBACK


__all__ = [
    "REASON_ACTIONS_PRESENT",
    "REASON_ATOMIC_ACTION",
    "REASON_ATOMIC_READ",
    "REASON_DIRECT",
    "REASON_DYNAMIC",
    "REASON_FALLBACK",
    "REASON_HIGH_RISK_APPROVAL",
    "REASON_SECURITY",
    "REASON_SEQUENTIAL",
    "REASON_SERVICE_MISSING",
    "REASON_TARGET_UNKNOWN",
    "REASON_WORKSPACE_MISSING",
    "ExecutionMode",
    "RouteDecision",
    "read_only_actions",
    "route",
]
