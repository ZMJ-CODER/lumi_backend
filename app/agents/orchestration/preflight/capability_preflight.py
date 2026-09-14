"""能力预检 Capability Preflight（方案 §3.3 / §3.4）。

模型调用**之前**依次校验（顺序即契约，不可跳步）：

1. 工作区绑定 → ``DEPENDENCY_MISSING_WORKSPACE``
2. Provider 存在与健康 → ``CAPABILITY_UNAVAILABLE`` / ``PROVIDER_UNHEALTHY``
3. 用户权限 → ``PERMISSION_DENIED``
4. 工具注册 → ``TOOL_NOT_REGISTERED``
5. 工具是否允许注入（窗口非空）→ ``CAPABILITY_UNAVAILABLE``
6. 是否需审批 → ``APPROVAL_REQUIRED``

硬约束（方案 §3.3 末段）：

* **预检失败不调用主模型、不给空工具列表**——结果里的 ``must_call_model=False`` 与
  ``tool_window=()`` 是给调用方的唯一判据，禁止"空工具列表 + 纯文本瞎答"；
* 结果状态与执行状态**分离**：预检只回答"能不能执行"，不回答"执行到哪一步"；
* 失败一律带上统一错误模型（:class:`~lumi_contracts.events.errors.UnifiedError`）：
  同一类失败在任何路径得到同一个 ``code`` + ``safe_message``；
* 插件被禁用 = 预检直接 ``CAPABILITY_UNAVAILABLE``（复用既有链路，不加新机制）。

本模块是**纯函数**（无 I/O）：调用方先把 workspace/provider/权限/工具注册/审批策略
探明再传进来，因此可以在不启动编排器的情况下被穷举测试。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from loguru import logger

from lumi_contracts import UnifiedError
from lumi_contracts.events.errors import translate_error


class PreflightStatus(StrEnum):
    """预检状态（方案 4 §3.3 的九态；与执行状态分离，前端按错误码分派）。

    :data:`FROZEN_PREFLIGHT_STATES` 是**对外冻结的四个失败状态**（前后端已确认），
    其余状态（``PROVIDER_UNHEALTHY`` / ``TOOL_NOT_REGISTERED`` / ``NEEDS_CLARIFICATION`` /
    ``SECURITY_BLOCKED``）是同一枚举里的细分状态，同样带统一错误码。
    """

    READY = "READY"
    DEPENDENCY_MISSING = "DEPENDENCY_MISSING"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    PROVIDER_UNHEALTHY = "PROVIDER_UNHEALTHY"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    TOOL_NOT_REGISTERED = "TOOL_NOT_REGISTERED"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    #: 安全策略明确禁止（硬拒：不给重试入口）。
    SECURITY_BLOCKED = "SECURITY_BLOCKED"
    # 兼容旧名（同一个状态，历史调用方仍可用）
    DEPENDENCY_MISSING_WORKSPACE = "DEPENDENCY_MISSING"

    @property
    def needs_human(self) -> bool:
        """需要人工介入（审批/澄清）——不是失败，但不能自动继续。"""
        return self in {
            PreflightStatus.APPROVAL_REQUIRED,
            PreflightStatus.NEEDS_CLARIFICATION,
        }


#: 对外冻结的失败状态（前端按这四个分派；``READY`` 表示可继续执行）。
FROZEN_PREFLIGHT_STATES: frozenset[str] = frozenset({
    PreflightStatus.DEPENDENCY_MISSING.value,
    PreflightStatus.CAPABILITY_UNAVAILABLE.value,
    PreflightStatus.PERMISSION_DENIED.value,
    PreflightStatus.APPROVAL_REQUIRED.value,
})

#: 硬拒状态：没有可执行的下一步，前端只展示原因（不给重试入口）。
PERMANENT_PREFLIGHT_STATES: frozenset[str] = frozenset({
    PreflightStatus.PERMISSION_DENIED.value,
    PreflightStatus.SECURITY_BLOCKED.value,
})

#: 预检状态 → 用户可以做的下一步（``safe_next_action``；硬拒状态为空）。
#: 两个键名都登记：契约侧规范名 ``DEPENDENCY_MISSING_WORKSPACE`` 与 App 侧历史值
#: ``DEPENDENCY_MISSING`` 是同一个状态，任何一侧读取都不得拿到空动作。
STATUS_NEXT_ACTIONS: dict[str, str] = {
    PreflightStatus.READY.value: "",
    PreflightStatus.DEPENDENCY_MISSING.value: "BIND_WORKSPACE",
    "DEPENDENCY_MISSING_WORKSPACE": "BIND_WORKSPACE",
    PreflightStatus.CAPABILITY_UNAVAILABLE.value: "ENABLE_CAPABILITY",
    PreflightStatus.PROVIDER_UNHEALTHY.value: "RETRY_PROVIDER",
    PreflightStatus.PERMISSION_DENIED.value: "",
    PreflightStatus.TOOL_NOT_REGISTERED.value: "UPGRADE_CLIENT",
    PreflightStatus.NEEDS_CLARIFICATION.value: "PROVIDE_TARGET",
    PreflightStatus.APPROVAL_REQUIRED.value: "APPROVE",
    PreflightStatus.SECURITY_BLOCKED.value: "",
}

#: 预检状态 → 统一错误码（``None`` = 可以继续执行）。
STATUS_ERROR_CODES: dict[str, str] = {
    PreflightStatus.READY.value: "",
    PreflightStatus.DEPENDENCY_MISSING.value: "DEPENDENCY_MISSING_WORKSPACE",
    PreflightStatus.CAPABILITY_UNAVAILABLE.value: "CAPABILITY_UNAVAILABLE",
    PreflightStatus.PROVIDER_UNHEALTHY.value: "PROVIDER_UNHEALTHY",
    PreflightStatus.PERMISSION_DENIED.value: "PERMISSION_DENIED",
    PreflightStatus.TOOL_NOT_REGISTERED.value: "TOOL_NOT_REGISTERED",
    PreflightStatus.NEEDS_CLARIFICATION.value: "TARGET_REQUIRED",
    PreflightStatus.APPROVAL_REQUIRED.value: "APPROVAL_REQUIRED",
    PreflightStatus.SECURITY_BLOCKED.value: "SECURITY_BLOCKED",
}

#: 动作意图 → 允许暴露给模型的工具窗口（方案 §3.4 的唯一映射表）。
#: 工具名必须与 ``app/agents/capabilities/catalog/legacy.py`` 的注册名一致（由
#: ``tests/orchestration/test_task_understanding_preflight.py`` 断言），否则预检会"通过"却注入不存在
#: 的工具，模型又只能自己编理由。
ACTION_TOOL_WINDOW: dict[str, tuple[str, ...]] = {
    "READ": ("workspace_navigator",),
    "SEARCH": ("workspace_navigator",),
    "CREATE": ("workspace_write",),
    "MODIFY": ("workspace_navigator", "workspace_edit"),
    "DELETE": ("workspace_navigator", "workspace_delete"),
    "MOVE": ("workspace_navigator", "workspace_move"),
    "EXECUTE": ("run_in_sandbox", "python_exec"),
    "SEND": ("send_email",),
    "PUBLISH": (),
}


@dataclass(frozen=True)
class PreflightCheck:
    """一次检查的留痕（排障 + 前端 process 事件都能用）。"""

    name: str
    ok: bool
    status: str = ""
    detail: str = ""


@dataclass(frozen=True)
class CapabilityPreflightResult:
    """预检结果：状态 + 统一错误 + 允许注入的工具窗口。"""

    status: str = PreflightStatus.READY.value
    error: UnifiedError | None = None
    tool_window: tuple[str, ...] = ()
    question: str = ""
    checks: tuple[PreflightCheck, ...] = field(default_factory=tuple)
    #: 本次预检覆盖的能力（插件启用状态校验的实际输入）。
    required_capabilities: tuple[str, ...] = ()
    #: 调用方给出的**更精确**下一步动作（空 = 用状态默认动作）。
    override_next_action: str = ""

    @property
    def ok(self) -> bool:
        return self.status == PreflightStatus.READY.value

    @property
    def must_call_model(self) -> bool:
        """硬约束：预检失败**不得**调用主模型。"""
        return self.ok

    @property
    def needs_human(self) -> bool:
        """需要人工介入（审批/澄清）：不是失败，但不能自动继续。"""
        try:
            return PreflightStatus(self.status).needs_human
        except ValueError:
            return False

    @property
    def permanent(self) -> bool:
        """硬拒（权限/安全）：没有可执行的下一步。"""
        return self.status in PERMANENT_PREFLIGHT_STATES

    @property
    def error_code(self) -> str:
        return STATUS_ERROR_CODES.get(self.status, "") or (self.error.code if self.error else "")

    @property
    def safe_message(self) -> str:
        return self.error.safe_message if self.error else ""

    @property
    def safe_next_action(self) -> str:
        """用户可以做的下一步（硬拒状态为空；未登记状态退回错误的建议动作）。"""
        if self.override_next_action:
            return self.override_next_action
        explicit = STATUS_NEXT_ACTIONS.get(self.status)
        if explicit is not None:
            return explicit
        return self.error.suggested_action if self.error else ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "error_code": self.error_code,
            "safe_message": self.safe_message,
            "safe_next_action": self.safe_next_action,
            "retryable": bool(self.error.retryable) if self.error else False,
            "needs_human": self.needs_human,
            "permanent": self.permanent,
            "must_call_model": self.must_call_model,
            "tool_window": list(self.tool_window),
            "required_capabilities": list(self.required_capabilities),
            "question": self.question,
            "checks": [{"name": item.name, "ok": item.ok, "detail": item.detail} for item in self.checks],
        }


def tool_window_for_actions(
    action_intents: Iterable[str] | None,
    *,
    registered_tools: Iterable[str] | None = None,
    profile: Any = None,
    resource_types: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """按动作意图给出工具窗口（画像驱动注入；关键词只作兜底，见 §3.2/§3.4）。

    给了 ``registered_tools`` 时只返回已注册的工具（不向模型暴露不存在的工具）。

    **三层真相源**（越靠后越新，各自带开关与兜底）：

    1. ``ACTION_TOOL_WINDOW``（静态表，默认路径，逐字不变）；
    2. ``TOOL_REGISTRY_DERIVED``（统一工具注册表派生，基于能力副作用）；
    3. ``RESOURCE_CAPABILITY_WINDOW``（**统一资源能力层**：动作意图 + 资源类型 →
       统一能力 → 候选 Provider → 规范工具）。资源层的窗口是前两者的**超集**，
       因此打开它只会"多出正确工具"，不会让既有工具消失。
    """
    window: list[str] = []
    for intent in action_intents or ():
        key = str(intent or "").strip().upper()
        static_tools = ACTION_TOOL_WINDOW.get(key, ())
        resolved = static_tools
        try:
            from app.agents.capabilities.catalog.tool_registry import action_window

            resolved = action_window(key, fallback=static_tools)
        except Exception as exc:  # noqa: BLE001 - 派生失败静默回到静态表
            logger.debug("[preflight] 工具窗口派生失败（回到静态表）: {}", str(exc)[:120])
        try:
            # 资源层（Phase 2）：只有开关打开时才参与；关闭时上面的结果原样返回。
            from app.agents.capabilities.policy.resource_window import (
                resource_types_for_profile,
                resource_window,
                window_enabled,
            )

            if window_enabled():
                resolved = resource_window(
                    [key],
                    resource_types=(
                        list(resource_types)
                        if resource_types is not None
                        else list(resource_types_for_profile(profile))
                    ),
                    fallback=resolved,
                )
        except Exception as exc:  # noqa: BLE001 - 资源层失败不能影响既有窗口
            logger.debug("[preflight] 资源能力窗口派生失败（保留上层结果）: {}", str(exc)[:120])
        for tool in resolved:
            if tool not in window:
                window.append(tool)
    if registered_tools is not None:
        allowed = {str(item) for item in registered_tools}
        window = [tool for tool in window if tool in allowed]
    return tuple(window)


def _profile_field(profile: Any, key: str, default: Any = None) -> Any:
    if isinstance(profile, Mapping):
        return profile.get(key, default)
    return getattr(profile, key, default)


def preflight_capabilities(
    *,
    profile: Any = None,
    workspace_bound: bool = True,
    requires_workspace: bool = False,
    required_capabilities: Sequence[str] | None = None,
    available_capabilities: Mapping[str, bool] | None = None,
    provider_health: Mapping[str, str] | None = None,
    permissions: Mapping[str, bool] | None = None,
    required_permission: str = "",
    registered_tools: Iterable[str] | None = None,
    desired_tools: Sequence[str] | None = None,
    approval_required: bool = False,
    target_clarity: str = "",
    security_blocked: bool = False,
    security_reason: str = "",
    next_action: str = "",
) -> CapabilityPreflightResult:
    """按固定顺序做各项检查；第一项失败即返回（不继续后面的检查）。

    顺序（方案 §3.2：先便宜的、先硬的）：

    1. 安全策略（``security_blocked``，硬拒）
    2. 目标澄清（``target_clarity == UNKNOWN`` 且有动作意图）
    3. 工作区绑定
    4. Provider / 能力提供方（含"插件被禁用/未安装"）
    5. 用户权限
    6. 工具注册 + 注入窗口非空
    7. 审批

    :param next_action: 调用方已知**更精确**的下一步（例如 Broker 给出"设备没连"时用
        ``CONNECT_PROVIDER`` 而不是通用的 ``RETRY_PROVIDER``）；留空则用状态默认值。
    """
    checks: list[PreflightCheck] = []
    actions = list(_profile_field(profile, "action_intents", []) or [])
    capabilities = list(required_capabilities or _profile_field(profile, "required_capabilities", []) or [])
    # 显式参数优先，其次读画像（画像才是唯一事实源，参数只用于测试/兜底）。
    clarity = str(target_clarity or _profile_field(profile, "target_clarity", "KNOWN") or "KNOWN").upper()
    registered = {str(item) for item in (registered_tools or ())}

    def fail(status: str, name: str, detail: str = "", question: str = "") -> CapabilityPreflightResult:
        checks.append(PreflightCheck(name=name, ok=False, status=status, detail=detail))
        return CapabilityPreflightResult(
            status=status,
            error=translate_error({"code": STATUS_ERROR_CODES.get(status, "system.internal")}),
            tool_window=(),
            question=question,
            checks=tuple(checks),
            required_capabilities=tuple(capabilities),
            # 调用方给的更精确动作优先（例如"设备没连" → CONNECT_PROVIDER）。
            override_next_action=str(next_action or ""),
        )

    # ⓪ 安全策略：硬拒优先于一切（连"要不要澄清"都不问）。
    if security_blocked:
        return fail(
            PreflightStatus.SECURITY_BLOCKED.value,
            "security_policy",
            str(security_reason or "安全策略拒绝该操作")[:120],
        )
    checks.append(PreflightCheck(name="security_policy", ok=True))

    # ① 澄清优先于执行：目标不清晰时不猜（既不是失败也不是成功）
    if clarity == "UNKNOWN" and actions:
        return fail(
            PreflightStatus.NEEDS_CLARIFICATION.value,
            "target_clarity",
            "目标未知但动作意图非空",
            question="请补充要操作的目标（文件/目录/对象）。",
        )
    checks.append(PreflightCheck(name="target_clarity", ok=True))

    # ② 工作区绑定
    need_workspace = bool(requires_workspace or _profile_field(profile, "requires_workspace", False))
    if need_workspace and not workspace_bound:
        return fail(PreflightStatus.DEPENDENCY_MISSING.value, "workspace_bound", "任务需要工作区但未绑定")
    checks.append(PreflightCheck(name="workspace_bound", ok=True))

    # ③ Provider / 能力提供方（含"插件被禁用"）
    availability = dict(available_capabilities or {})
    health = dict(provider_health or {})
    for capability in capabilities:
        if availability.get(capability) is False:
            return fail(
                PreflightStatus.CAPABILITY_UNAVAILABLE.value,
                f"capability:{capability}",
                "能力提供方未启用（插件被禁用/未安装）",
            )
        state = str(health.get(capability, "") or "").strip().casefold()
        if state in {"unhealthy", "offline", "error"}:
            return fail(PreflightStatus.PROVIDER_UNHEALTHY.value, f"provider:{capability}", state)
    checks.append(PreflightCheck(name="capability_available", ok=True))

    # ④ 用户权限
    permission = str(required_permission or _profile_field(profile, "required_permission", "") or "")
    if permission and permissions is not None and not permissions.get(permission, False):
        return fail(PreflightStatus.PERMISSION_DENIED.value, f"permission:{permission}", "权限校验未通过")
    checks.append(PreflightCheck(name="permission", ok=True))

    # ⑤ 工具注册 + 注入窗口非空（禁止"空工具列表 + 纯文本瞎答"）
    window = tool_window_for_actions(actions, registered_tools=registered_tools, profile=profile)
    if desired_tools:
        missing = [str(tool) for tool in desired_tools if registered_tools is not None and str(tool) not in registered]
        if missing:
            return fail(PreflightStatus.TOOL_NOT_REGISTERED.value, "tool_registered", f"未注册：{missing}")
        window = tuple(dict.fromkeys([*window, *[str(tool) for tool in desired_tools]]))
    if actions and registered_tools is not None and not window:
        return fail(PreflightStatus.CAPABILITY_UNAVAILABLE.value, "tool_window", "动作意图非空但没有可注入的工具")
    checks.append(PreflightCheck(name="tool_window", ok=True, detail=",".join(window)))

    # ⑥ 审批
    if approval_required or bool(_profile_field(profile, "approval_required", False)):
        return fail(PreflightStatus.APPROVAL_REQUIRED.value, "approval", "高风险操作需人工审批")
    checks.append(PreflightCheck(name="approval", ok=True))

    return CapabilityPreflightResult(
        status=PreflightStatus.READY.value,
        tool_window=window,
        checks=tuple(checks),
        required_capabilities=tuple(capabilities),
    )


def build_tool_window(
    *,
    profile: Any = None,
    action_intents: Iterable[str] | None = None,
    registered_tools: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """按画像给出可注入的工具窗口（执行器/计划编译共用；不重新解析用户原文）。

    ``action_intents`` 显式给出时优先（调用方已从画像取出），否则读画像。
    给了 ``registered_tools`` 时会剔除未注册工具——绝不把不存在的工具交给模型。
    """
    actions = list(action_intents or _profile_field(profile, "action_intents", []) or [])
    return tool_window_for_actions(actions, registered_tools=registered_tools, profile=profile)


#: 预检覆盖的能力 vs 画像声明的能力：预检**只看**画像声明的抽象能力，
#: 不替画像"补"能力（补能力属于画像职责，越界会让影子比对失去意义）。
def declared_capabilities(profile: Any) -> tuple[str, ...]:
    return tuple(str(item) for item in (_profile_field(profile, "required_capabilities", []) or []) if str(item))


__all__ = [
    "ACTION_TOOL_WINDOW",
    "FROZEN_PREFLIGHT_STATES",
    "PERMANENT_PREFLIGHT_STATES",
    "STATUS_ERROR_CODES",
    "STATUS_NEXT_ACTIONS",
    "CapabilityPreflightResult",
    "PreflightCheck",
    "PreflightStatus",
    "build_tool_window",
    "declared_capabilities",
    "preflight_capabilities",
    "tool_window_for_actions",
]

