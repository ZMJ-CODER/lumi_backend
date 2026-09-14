"""``CapabilityPreflight``：执行前能力预检的**契约结论**（方案 4 §3.3）。

预检回答的是"这件事现在干得了吗"，与"执行到哪一步了"严格分开：

* 状态是**稳定枚举**（9 个），前端按状态分派交互（给选项 / 提示修环境 / 走审批 / 硬拒）；
* 失败一律带统一错误码（``UnifiedError`` 口径）+ ``safe_message`` + ``safe_next_action``；
* ``must_call_model`` 是给调用方的**唯一判据**：假即"不要调用主模型"——禁止把空工具
  列表丢给模型让它自己编一段"我无法创建文件"。

本模块只放数据结构与枚举映射，判定逻辑在 ``app.agents.orchestration.preflight.capability_preflight``。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class PreflightState(StrEnum):
    """预检状态（方案 §3.3）。**与执行终态严格区分**，不得混用。

    九态与方案逐字一致。历史值 ``DEPENDENCY_MISSING`` 是同一状态的旧名，由
    ``PREFLIGHT_STATE_ALIASES`` 归一——枚举里不再重复声明，避免导出的 TS 联合类型出现
    重复字面量。
    """

    READY = "READY"
    #: 需要工作区但未绑定。
    DEPENDENCY_MISSING_WORKSPACE = "DEPENDENCY_MISSING_WORKSPACE"
    #: 能力离线 / 插件被禁用 / 未安装。
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    #: Provider 不健康（在线但不可用）。
    PROVIDER_UNHEALTHY = "PROVIDER_UNHEALTHY"
    #: 用户无权限（硬拒，不给重试入口）。
    PERMISSION_DENIED = "PERMISSION_DENIED"
    #: 工具未注册（多为客户端版本旧）。
    TOOL_NOT_REGISTERED = "TOOL_NOT_REGISTERED"
    #: 目标不明确：**用户没说清要什么** → 前端给选项，不是失败。
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    #: 能力可用但需要用户确认 → 走审批卡。
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    #: 安全策略明确禁止（硬拒，不给重试入口）。
    SECURITY_BLOCKED = "SECURITY_BLOCKED"

    @property
    def ok(self) -> bool:
        return self is PreflightState.READY

    @property
    def blocks_model(self) -> bool:
        """是否必须阻断模型调用（``READY`` 之外全部阻断，含澄清/审批）。"""
        return not self.ok

    @property
    def needs_human(self) -> bool:
        """是否需要人工介入（审批 / 澄清）——不是失败，但不能自动继续。"""
        return self in {PreflightState.APPROVAL_REQUIRED, PreflightState.NEEDS_CLARIFICATION}


#: 预检状态的**旧名别名**（历史输出/历史快照 → 规范状态）。
PREFLIGHT_STATE_ALIASES: dict[str, str] = {
    "DEPENDENCY_MISSING": PreflightState.DEPENDENCY_MISSING_WORKSPACE.value,
}


def normalize_preflight_state(value: object) -> PreflightState | None:
    """任意来源的状态串 → 契约状态（别名归一；无法识别返回 ``None``，不猜）。"""
    text = str(getattr(value, "value", value) or "").strip().upper()
    if not text:
        return None
    text = PREFLIGHT_STATE_ALIASES.get(text, text)
    try:
        return PreflightState(text)
    except ValueError:
        return None


#: 预检状态 → 统一错误码（空串表示成功）。全部取自 ``UnifiedError`` 已登记码表。
PREFLIGHT_ERROR_CODES: dict[str, str] = {
    PreflightState.READY.value: "",
    PreflightState.DEPENDENCY_MISSING_WORKSPACE.value: "DEPENDENCY_MISSING_WORKSPACE",
    PreflightState.CAPABILITY_UNAVAILABLE.value: "CAPABILITY_UNAVAILABLE",
    PreflightState.PROVIDER_UNHEALTHY.value: "PROVIDER_UNHEALTHY",
    PreflightState.PERMISSION_DENIED.value: "PERMISSION_DENIED",
    PreflightState.TOOL_NOT_REGISTERED.value: "TOOL_NOT_REGISTERED",
    PreflightState.NEEDS_CLARIFICATION.value: "TARGET_REQUIRED",
    PreflightState.APPROVAL_REQUIRED.value: "APPROVAL_REQUIRED",
    PreflightState.SECURITY_BLOCKED.value: "SECURITY_BLOCKED",
}
#: 统一错误码 → 预检状态（反向映射；同码只能属于一个状态）。
PREFLIGHT_STATE_BY_ERROR: dict[str, str] = {
    code: state for state, code in PREFLIGHT_ERROR_CODES.items() if code
}

#: 预检状态的**用户可执行下一步**（``safe_next_action``；硬拒状态为空 = 不给重试入口）。
PREFLIGHT_NEXT_ACTIONS: dict[str, str] = {
    PreflightState.READY.value: "",
    PreflightState.DEPENDENCY_MISSING_WORKSPACE.value: "BIND_WORKSPACE",
    PreflightState.CAPABILITY_UNAVAILABLE.value: "ENABLE_CAPABILITY",
    PreflightState.PROVIDER_UNHEALTHY.value: "RETRY_PROVIDER",
    PreflightState.PERMISSION_DENIED.value: "",
    PreflightState.TOOL_NOT_REGISTERED.value: "UPGRADE_CLIENT",
    PreflightState.NEEDS_CLARIFICATION.value: "PROVIDE_TARGET",
    PreflightState.APPROVAL_REQUIRED.value: "APPROVE",
    PreflightState.SECURITY_BLOCKED.value: "",
}

#: 硬拒状态（不给重试入口；前端只展示原因）。
PERMANENT_PREFLIGHT_STATES: frozenset[str] = frozenset(
    {PreflightState.PERMISSION_DENIED.value, PreflightState.SECURITY_BLOCKED.value}
)

#: 澄清选项（方案 §6.1：``control`` 事件携带的 options）。
CLARIFICATION_OPTIONS: tuple[str, ...] = (
    "GENERATE_ONLY",
    "CREATE_NEW",
    "EDIT_EXISTING",
    "CANCEL_ACTION",
)


class CapabilityPreflight(BaseModel):
    """预检结论（可落 Job 快照、可下发给前端）。"""

    state: PreflightState = PreflightState.READY
    error_code: str = ""
    safe_message: str = ""
    safe_next_action: str = ""
    #: 需要澄清时的问题与选项（前端只展示，不自己判断）。
    question: str = ""
    options: list[str] = Field(default_factory=list)
    #: 允许注入模型的工具窗口（缩到最小集）。
    tool_window: list[str] = Field(default_factory=list)
    #: 是否允许调用主模型（**唯一判据**）。
    must_call_model: bool = True
    #: 检查留痕（名称 + 是否通过；不含内部细节）。
    checks: list[dict] = Field(default_factory=list)
    #: 覆盖到的能力（插件启用状态校验结果，见 §3.4）。
    required_capabilities: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.state.ok

    @property
    def needs_human(self) -> bool:
        return self.state.needs_human

    def as_dict(self) -> dict:
        return self.model_dump(mode="json", exclude_none=True)


__all__ = [
    "CLARIFICATION_OPTIONS",
    "PERMANENT_PREFLIGHT_STATES",
    "PREFLIGHT_ERROR_CODES",
    "PREFLIGHT_NEXT_ACTIONS",
    "PREFLIGHT_STATE_ALIASES",
    "PREFLIGHT_STATE_BY_ERROR",
    "CapabilityPreflight",
    "PreflightState",
    "normalize_preflight_state",
]
