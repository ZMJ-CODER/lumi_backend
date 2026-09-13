"""CapabilityPreflightService：Orchestrator 的**唯一预检入口**（方案 §3.3 职责收口）。

职责划分（用户已拍板）：

* ``task_preflight``：保留为兼容入口，内部转调本服务；
* ``broker._preflight_failure``：只负责给出**底层失败原因**（能力/租约/健康探测结果），
  不直接决定用户可见结果；
* **本服务**：把底层原因收敛成对外冻结的四个状态 + UnifiedError + 工具窗口；
* ``Orchestrator``：按结果决定继续 / 请求审批 / 阻断（禁止拿空工具列表继续）。

灰度：``CAPABILITY_PREFLIGHT_V2`` 关闭时 ``enabled()`` 返回 False，调用方走旧路径；
本模块不做任何 I/O，底层探测由调用方以 ``probe`` 回调注入，因此可离线穷举测试。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from loguru import logger

from app.agents.orchestration.capability_preflight import (
    CapabilityPreflightResult,
    PreflightStatus,
    preflight_capabilities,
)

FLAG = "CAPABILITY_PREFLIGHT_V2"

#: 底层探测结果 → 对外冻结状态（Broker 只报事实，映射在这里）。
LOW_LEVEL_TO_STATUS: dict[str, str] = {
    "capability_unavailable": PreflightStatus.CAPABILITY_UNAVAILABLE.value,
    "provider_unhealthy": PreflightStatus.PROVIDER_UNHEALTHY.value,
    "permission_denied": PreflightStatus.PERMISSION_DENIED.value,
    "tool_not_registered": PreflightStatus.TOOL_NOT_REGISTERED.value,
    "workspace_missing": PreflightStatus.DEPENDENCY_MISSING.value,
    "approval_required": PreflightStatus.APPROVAL_REQUIRED.value,
    # 能力**已声明**但没有可用 Provider 的三种可执行原因（Broker 给出更细的事实）：
    # 都归入"提供方不可用"这一类（可重试），但下一步文案按原因区分——
    # "设备没连"、"工作区绑错"、"位置不允许" 的正确动作完全不同。
    "provider_not_connected": PreflightStatus.PROVIDER_UNHEALTHY.value,
    "provider_binding_mismatch": PreflightStatus.PROVIDER_UNHEALTHY.value,
    "provider_unroutable": PreflightStatus.PROVIDER_UNHEALTHY.value,
}

#: 底层事实 → 更精确的下一步（覆盖 ``PREFLIGHT_NEXT_ACTIONS`` 的通用文案）。
FACT_NEXT_ACTIONS: dict[str, str] = {
    "provider_not_connected": "CONNECT_PROVIDER",
    "provider_binding_mismatch": "BIND_WORKSPACE",
    "provider_unroutable": "CHANGE_EXECUTION_PLACEMENT",
}


class BrokerProbe(Protocol):
    """底层探测回调：只回答事实，不做用户可见判定。"""

    def __call__(self, capabilities: Sequence[str]) -> Mapping[str, str]:
        """返回 ``{capability: 底层原因}``（无问题的能力可以不出现在结果里）。"""


class CapabilityPreflightService:
    """唯一预检入口（``enabled()`` 为假时调用方保持旧路径）。"""

    def __init__(self, *, settings: Any = None) -> None:
        self._settings = settings

    def enabled(self) -> bool:
        from app.core.feature_flags import feature_enabled

        return feature_enabled(FLAG, settings=self._settings)

    def preflight(
        self,
        *,
        profile: Any = None,
        probe: BrokerProbe | None = None,
        workspace_bound: bool = True,
        requires_workspace: bool = False,
        permissions: Mapping[str, bool] | None = None,
        required_permission: str = "",
        registered_tools: Sequence[str] | None = None,
        desired_tools: Sequence[str] | None = None,
        approval_required: bool = False,
        security_blocked: bool = False,
        security_reason: str = "",
    ) -> CapabilityPreflightResult:
        """按底层探测结果做各项检查（顺序与冻结状态见 capability_preflight）。"""
        capabilities = list(
            (profile.get("required_capabilities") if isinstance(profile, Mapping) else
             getattr(profile, "required_capabilities", [])) or []
        )
        health: dict[str, str] = {}
        available: dict[str, bool] = {}
        # 记住触发失败的底层事实：对外状态由本服务决定，但"下一步"要按事实说准
        # （设备没连 → CONNECT_PROVIDER；工作区绑错 → BIND_WORKSPACE）。
        fact_next_action = ""
        if probe is not None and capabilities:
            for capability, reason in dict(probe(capabilities)).items():
                status = LOW_LEVEL_TO_STATUS.get(str(reason), "")
                if status in {"", PreflightStatus.PROVIDER_UNHEALTHY.value}:
                    available[str(capability)] = True
                    if status:
                        health[str(capability)] = "unhealthy"
                    if not fact_next_action:
                        fact_next_action = FACT_NEXT_ACTIONS.get(str(reason), "")
                else:
                    available[str(capability)] = False
        else:
            # 没有注入 probe 时用**插件启用状态**兜底（方案 §3.4 吸收 #1）：
            # 被 disable/uninstall 的插件其能力直接 CAPABILITY_UNAVAILABLE，
            # 否则"插件被禁用"会被误判成"能力可用"。
            available = plugin_capability_availability(capabilities) if capabilities else {}
        return preflight_capabilities(
            profile=profile,
            workspace_bound=workspace_bound,
            requires_workspace=requires_workspace,
            required_capabilities=capabilities or None,
            available_capabilities=available or None,
            provider_health=health or None,
            permissions=permissions,
            required_permission=required_permission,
            registered_tools=registered_tools,
            desired_tools=desired_tools,
            approval_required=approval_required,
            security_blocked=security_blocked,
            security_reason=security_reason,
            next_action=fact_next_action,
        )


def plugin_capability_availability(capabilities: Sequence[str]) -> dict[str, bool]:
    """能力名 → 是否**在目录/注册表里有声明**（方案 §3.4 吸收 #1）。

    **注意语义**：这里回答的是"这个能力名有没有被任何实现声明过"，而不是"现在能不能
    调用"——后者（有没有可用的 Provider 租约）只有 Broker 探测能回答，绝不能靠静态
    注册表猜。预检里凡是携带 probe 的调用点都必须用 probe，本函数只作无 probe 时的
    兜底，且只在**未声明**时才判不可用。

    能力名必须先归一化：目录里存的是无版本基名（``workspace.read``），而画像解析出的
    具体能力名带契约版本（``workspace.read@1``）；直接按字符串比对会把同一个能力判成
    两个结果——曾因此把只读任务误报成"缺少必需能力"。
    """
    from app.agents.capabilities.resolver import normalize_capability_name

    wanted = [str(item) for item in capabilities if str(item)]
    if not wanted:
        return {}
    known: set[str] = set()
    try:
        from app.agents.capabilities.builtin import TOOL_CAPABILITY_MAP

        known |= {str(value) for value in TOOL_CAPABILITY_MAP.values() if value}
    except Exception:  # noqa: BLE001 - 表不可用时不猜
        pass
    try:
        from app.agents.capabilities.catalog import IMPLEMENTATION_MAP

        known |= {str(value) for value in IMPLEMENTATION_MAP.values() if value}
    except Exception:  # noqa: BLE001
        pass
    if not known:
        return {}
    return {capability: normalize_capability_name(capability) in known for capability in wanted}


def tool_registration_facts() -> tuple[str, ...]:
    """当前**真的可调用**的工具名（预检第 6 步的输入；不可用时返回空元组 = 不校验）。

    三部分并集，缺任何一部分都会出现一类真实错误：

    * 进程内已注册的 Skill（含**插件工具**）——只看静态表会把插件工具判成"不存在"，
      于是声明了新能力的插件工具永远进不了动作窗口；
    * 静态 ``TOOL_CAPABILITY_MAP`` —— 客户端原子工具（服务端没有同名 Skill，
      但模型确实调得到，预检窗口一直允许它们）；
    * 影子注册表 ``ToolSpec`` —— 安装/升级写入的声明（与 Skill 表通常重合，作兜底）。

    ⚠️ 动作窗口的**静态对齐部分**刻意不用这个函数：它必须与静态表逐条相等（可对拍），
    因此那里用"静态表认得这个名字"作为存在性判据（见
    ``tool_registry._is_statically_known_tool``）。
    """
    names: list[str] = []
    try:
        from app.agents.capabilities.builtin import TOOL_CAPABILITY_MAP

        names.extend(str(item) for item in TOOL_CAPABILITY_MAP)
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.agents.skills.registry import ToolRegistry

        names.extend(str(getattr(tool, "name", "") or "") for tool in ToolRegistry.list())
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.contracts.tools import export_tool_specs

        names.extend(str(key).rsplit(".", 1)[-1] for key in export_tool_specs())
    except Exception:  # noqa: BLE001
        pass
    return tuple(dict.fromkeys(name for name in names if name))


#: 预检结论在 Job 快照/事件里的字段名（前端按此读取；缺失即隐藏）。
PREFLIGHT_SNAPSHOT_KEY = "preflight"
#: 预检失败时的过程事件：放在 routing 里的键 + 稳定去重键（SSE 帧与刷新投影同一行）。
PREFLIGHT_NOTICE_KEY = "preflight_notice"
PREFLIGHT_NOTICE_ENTRY_ID = "process:preflight"


def preflight_snapshot(result: CapabilityPreflightResult) -> dict[str, Any]:
    """预检结论 → 可落 Job 快照 / 可下发前端的**结构化状态**。

    只给结论与原因码，不含内部探测细节；前端据此显示"缺什么能力 / 为什么被阻断 / 等谁确认"，
    不需要自己判断能力是否可用。方案 4 §3.5：失败时还要给出 ``safe_next_action``
    （用户可以做的下一步）与 ``required_capabilities``（缺的是哪些能力）。
    """
    return {
        "status": result.status,
        "ok": bool(result.ok),
        "error_code": result.error_code,
        "safe_message": result.safe_message,
        "safe_next_action": result.safe_next_action,
        "retryable": bool(result.error.retryable) if result.error else False,
        "next_action": result.safe_next_action,
        "needs_human": bool(result.needs_human),
        "permanent": bool(result.permanent),
        "question": result.question,
        "tool_window": list(result.tool_window),
        "required_capabilities": list(result.required_capabilities),
        "must_call_model": bool(result.must_call_model),
        "checks": [{"name": item.name, "ok": item.ok} for item in result.checks],
    }


def attach_preflight(routing: Mapping[str, Any] | None, result: CapabilityPreflightResult) -> dict[str, Any]:
    """把预检结论并入 ``job.routing``（写入路径唯一入口；返回新字典，不改原对象）。"""
    merged = dict(routing or {})
    merged[PREFLIGHT_SNAPSHOT_KEY] = preflight_snapshot(result)
    return merged


def preflight_process_notice(result: CapabilityPreflightResult) -> dict[str, Any] | None:
    """预检失败时的 process 事件载荷（正文类事件不带内部字段）。"""
    if result.ok:
        return None
    return {
        "kind": "thinking",
        "title": "能力预检",
        "summary": result.safe_message or "当前任务无法继续",
        "status": "failed",
        "detail": result.error_code,
    }


#: 预检状态 → ``control`` 帧的 ``state``（方案 4 §6.1；不新增事件类型）。
CONTROL_STATE_BY_PREFLIGHT: dict[str, str] = {
    # 目标不明确：等用户补充，不是失败。
    PreflightStatus.NEEDS_CLARIFICATION.value: "waiting_clarification",
    # 能力可用但要确认：走审批卡。
    PreflightStatus.APPROVAL_REQUIRED.value: "waiting_approval",
    # 其余失败一律 blocked（含工作区未绑定 / 能力不可用 / 无权限 / 安全阻断）。
    PreflightStatus.DEPENDENCY_MISSING.value: "blocked",
    PreflightStatus.CAPABILITY_UNAVAILABLE.value: "blocked",
    PreflightStatus.PROVIDER_UNHEALTHY.value: "blocked",
    PreflightStatus.PERMISSION_DENIED.value: "blocked",
    PreflightStatus.TOOL_NOT_REGISTERED.value: "blocked",
    PreflightStatus.SECURITY_BLOCKED.value: "blocked",
}

#: 澄清状态的选项（方案 §6.1：``control`` 帧携带 options，前端只展示）。
CLARIFICATION_OPTIONS: tuple[str, ...] = (
    "GENERATE_ONLY",
    "CREATE_NEW",
    "EDIT_EXISTING",
    "CANCEL_ACTION",
)


def control_state_for_preflight(result: CapabilityPreflightResult) -> str:
    """预检结论 → ``control.state``（``READY`` 返回空串 = 不发控制帧）。"""
    if result.ok:
        return ""
    return CONTROL_STATE_BY_PREFLIGHT.get(result.status, "blocked")


def preflight_control_frame(result: CapabilityPreflightResult) -> dict[str, Any] | None:
    """预检失败 → ``control`` 事件载荷（方案 §6.1：**走既有事件体系**）。

    三种语义严格区分（前端据此分派交互）：

    * ``blocked``：硬阻断（环境/权限/安全）→ 按 ``error_code`` 给可执行下一步；
    * ``waiting_clarification``：目标不明确 → 给选项（仅生成/创建/编辑/取消）；
    * ``waiting_approval``：能力可用但需确认 → 与既有 ``approval_required`` 事件并存。

    ``READY`` 时返回 ``None``（不发帧）。只带结构化字段，不含内部探测细节。
    """
    state = control_state_for_preflight(result)
    if not state:
        return None
    frame: dict[str, Any] = {
        "type": "control",
        "state": state,
        "phase": "preflight",
        "error_code": result.error_code,
        "safe_message": result.safe_message,
        "safe_next_action": result.safe_next_action,
        "next_action": result.safe_next_action,
        "reason_code": result.error_code,
        "reason": result.safe_message,
        "question": result.question,
        "required_capabilities": list(result.required_capabilities),
        "tool_window": list(result.tool_window),
        "must_call_model": bool(result.must_call_model),
        "retryable": bool(result.error.retryable) if result.error else False,
    }
    if state == "waiting_clarification":
        frame["options"] = list(CLARIFICATION_OPTIONS)
    return frame


# ── 提交期能力解析快照的时效性 ────────────────────────────────
#: 解析结论的落库时刻（ISO8601）。前端据此说明"这是提交时的结论"。
CAPABILITY_RESOLUTION_AT_KEY = "capability_resolution_at"
#: 重算结论所需的输入（required/binding）：读取路径靠它刷新快照。
CAPABILITY_RESOLUTION_QUERY_KEY = "capability_resolution_query"
#: 读取路径重新解析过的时刻：与 ``..._at`` 不同即表示快照已被刷新。
CAPABILITY_RESOLUTION_REFRESHED_KEY = "capability_resolution_refreshed_at"


def attach_capability_resolution_time(
    payload: dict[str, Any], *, timestamp: str = ""
) -> dict[str, Any]:
    """给 ``RequiredCapabilitiesReport.as_dict()`` 盖上**时点**戳。

    没有时刻戳的话，提交期快照与读取期的实时结论在前端长得一模一样，
    "客户端已经连上、任务其实能读"这种后来才成立的事实无法与陈旧结论区分。
    """
    from datetime import datetime, timezone

    stamp = str(timestamp or "").strip() or datetime.now(timezone.utc).isoformat()
    return {**dict(payload or {}), CAPABILITY_RESOLUTION_AT_KEY: stamp}


async def refresh_capability_resolution(
    routing: dict[str, Any],
    *,
    broker: Any = None,
    lease_service: Any = None,
) -> dict[str, Any]:
    """按当前租约**重新解析**提交期写下的能力结论（原地更新并返回 ``routing``）。

    为什么需要：``capability_resolution`` 只在提交时写一次，而客户端 Provider 是
    异步注册/心跳的 —— 提交那一刻"没有可用的 Provider"完全可能在一秒后变成可用。
    不刷新就会出现"界面报缺能力、同一步骤读得好好的"这种自相矛盾。

    失败一律降级：保留旧快照并记录 ``capability_resolution_error``，绝不因为刷新
    失败把任务详情/提交打挂。
    """
    query = routing.get(CAPABILITY_RESOLUTION_QUERY_KEY)
    if not isinstance(query, dict):
        return routing
    required = [str(item) for item in (query.get("required") or []) if str(item)]
    if not required:
        return routing
    try:
        from lumi_contracts.plugins import SessionBinding

        from app.agents.capabilities.resolver import CapabilityResolver

        if lease_service is not None:
            refresh = getattr(lease_service, "refresh_from_redis", None)
            if callable(refresh):
                try:
                    await refresh()
                except Exception as exc:  # noqa: BLE001 - 同步失败沿用本地副本
                    logger.debug("[capability] 租约同步失败（沿用本地副本）: {}", str(exc)[:120])
        if broker is None:
            from app.agents.capabilities.broker import capability_broker

            broker = capability_broker
        report = CapabilityResolver(broker=broker).resolve(
            required,
            binding=SessionBinding(
                user_id=str(query.get("user_id") or ""),
                conversation_id=str(query.get("conversation_id") or ""),
                workspace_id=str(query.get("workspace_id") or ""),
            ),
        )
        from datetime import datetime, timezone

        stamp = datetime.now(timezone.utc).isoformat()
        previous_at = str((routing.get("capability_resolution") or {}).get(
            CAPABILITY_RESOLUTION_AT_KEY, ""
        ) or "")
        routing["capability_resolution"] = attach_capability_resolution_time(
            report.as_dict(), timestamp=stamp
        )
        routing[CAPABILITY_RESOLUTION_REFRESHED_KEY] = stamp
        if previous_at and previous_at != stamp:
            # 结论变化过：留一条可审计的痕迹（前端可提示"已刷新"而不是沉默改口）。
            routing["capability_resolution_previous_at"] = previous_at
        routing.pop("capability_resolution_error", None)
    except Exception as exc:  # noqa: BLE001 - 刷新失败保留旧快照
        logger.warning("能力解析刷新失败（保留提交期快照）: {}", str(exc)[:160])
        routing["capability_resolution_error"] = str(exc)[:160]
    return routing


__all__ = [
    "CAPABILITY_RESOLUTION_AT_KEY",
    "CAPABILITY_RESOLUTION_QUERY_KEY",
    "CAPABILITY_RESOLUTION_REFRESHED_KEY",
    "CLARIFICATION_OPTIONS",
    "CONTROL_STATE_BY_PREFLIGHT",
    "FACT_NEXT_ACTIONS",
    "FLAG",
    "LOW_LEVEL_TO_STATUS",
    "PREFLIGHT_NOTICE_ENTRY_ID",
    "PREFLIGHT_NOTICE_KEY",
    "PREFLIGHT_SNAPSHOT_KEY",
    "BrokerProbe",
    "CapabilityPreflightService",
    "attach_preflight",
    "attach_capability_resolution_time",
    "control_state_for_preflight",
    "plugin_capability_availability",
    "preflight_control_frame",
    "preflight_process_notice",
    "preflight_snapshot",
    "refresh_capability_resolution",
    "tool_registration_facts",
]

