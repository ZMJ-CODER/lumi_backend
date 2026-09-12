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

from lumi_contracts import UnifiedError
from lumi_contracts.events.errors import translate_error


class PreflightStatus(StrEnum):
    """预检状态（与执行状态分离；前端只按其中的错误码分派）。

    :data:`FROZEN_PREFLIGHT_STATES` 是**对外冻结的四个失败状态**（前后端已确认）：
    其余状态（``TOOL_NOT_REGISTERED`` / ``NEEDS_CLARIFICATION``）保留给内部与文案细化，
    但对外一律能用这四个之一表达。
    """

    READY = "READY"
    DEPENDENCY_MISSING = "DEPENDENCY_MISSING"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    PROVIDER_UNHEALTHY = "PROVIDER_UNHEALTHY"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    TOOL_NOT_REGISTERED = "TOOL_NOT_REGISTERED"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    # 兼容旧名（同一个状态，历史调用方仍可用）
    DEPENDENCY_MISSING_WORKSPACE = "DEPENDENCY_MISSING"


#: 对外冻结的失败状态（前端按这四个分派；``READY`` 表示可继续执行）。
FROZEN_PREFLIGHT_STATES: frozenset[str] = frozenset({
    PreflightStatus.DEPENDENCY_MISSING.value,
    PreflightStatus.CAPABILITY_UNAVAILABLE.value,
    PreflightStatus.PERMISSION_DENIED.value,
    PreflightStatus.APPROVAL_REQUIRED.value,
})

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
}

#: 动作意图 → 允许暴露给模型的工具窗口（方案 §3.4 的唯一映射表）。
ACTION_TOOL_WINDOW: dict[str, tuple[str, ...]] = {
    "READ": ("workspace_navigator",),
    "SEARCH": ("workspace_navigator",),
    "CREATE": ("workspace_write",),
    "MODIFY": ("workspace_navigator", "workspace_edit"),
    "DELETE": ("workspace_navigator", "workspace_delete"),
    "MOVE": ("workspace_navigator", "workspace_move"),
    "EXECUTE": ("sandbox_run_in_sandbox", "sandbox_python_exec"),
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

    @property
    def ok(self) -> bool:
        return self.status == PreflightStatus.READY.value

    @property
    def must_call_model(self) -> bool:
        """硬约束：预检失败**不得**调用主模型。"""
        return self.ok

    @property
    def error_code(self) -> str:
        return STATUS_ERROR_CODES.get(self.status, "") or (self.error.code if self.error else "")

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "error_code": self.error_code,
            "safe_message": self.error.safe_message if self.error else "",
            "tool_window": list(self.tool_window),
            "question": self.question,
            "checks": [{"name": item.name, "ok": item.ok, "detail": item.detail} for item in self.checks],
        }


def tool_window_for_actions(
    action_intents: Iterable[str] | None,
    *,
    registered_tools: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """按动作意图给出工具窗口（画像驱动注入；关键词只作兜底，见 §3.2/§3.4）。

    给了 ``registered_tools`` 时只返回已注册的工具（不向模型暴露不存在的工具）。
    """
    window: list[str] = []
    for intent in action_intents or ():
        for tool in ACTION_TOOL_WINDOW.get(str(intent or "").strip().upper(), ()):
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
) -> CapabilityPreflightResult:
    """按固定顺序做六项检查；第一项失败即返回（不继续后面的检查）。"""
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
        )

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
    window = tool_window_for_actions(actions, registered_tools=registered_tools)
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
        status=PreflightStatus.READY.value, tool_window=window, checks=tuple(checks)
    )


__all__ = [
    "ACTION_TOOL_WINDOW",
    "FROZEN_PREFLIGHT_STATES",
    "STATUS_ERROR_CODES",
    "CapabilityPreflightResult",
    "PreflightCheck",
    "PreflightStatus",
    "preflight_capabilities",
    "tool_window_for_actions",
]

