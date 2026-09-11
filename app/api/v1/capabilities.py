"""阶段 2 REST：Provider 能力注册 / 心跳 / 注销 / 能力目录。

客户端（Electron）在这里声明"本机此刻能提供哪些能力"，服务端据此签发租约；租约
过期或注销会同步摘除能力注册表，Broker 因此**不会**再选到离线 Provider。

三条实现约束：

* **主体来自鉴权**：``user_id`` 一律取 token（``payload["sub"]``），请求体里同名字段
  只用于核对，防止替别人注册/续期能力；
* **结构化错误**：位置不允许（本地能力注册到服务端）、能力未声明、租约过期都返回
  稳定错误码，而不是 500 或"工具调用失败"；
* 本接口**只注册与生命周期**，不执行能力（执行走 Broker / 既有执行路径）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from app.core.deps import require_auth
from app.core.exceptions import BadRequestException, ForbiddenException
from lumi_contracts.plugins import CapabilityErrorCode

from app.services.capability_lease import (
    CapabilityLeaseService,
    LeaseRejected,
    capability_lease_service,
    clamp_ttl,
)

router = APIRouter()

#: 共享租约服务（与撤销订阅、Broker 用同一实例：清缓存必须清到同一份）。
lease_service: CapabilityLeaseService = capability_lease_service

#: 与客户端租约自算默认值保持一致（客户端在缺 ``expires_at`` 时按 now+120s 自算）。
CLIENT_LEASE_TTL_SECONDS = 120.0


class CapabilityDeclaration(BaseModel):
    """一条能力声明（能力名 + 契约版本）。

    客户端会**额外**带上 ``provider_id`` / ``provider_version`` / ``data_locality`` /
    ``requires_approval`` / ``health_status`` 等字段（扁平兼容写法）。服务端全部接受但
    **不采信**：本地性与审批要求以服务端能力目录为准，客户端声明只能"更保守"，不能放宽。
    """

    model_config = ConfigDict(extra="allow")

    capability: str = Field(..., min_length=3, max_length=160, description="如 workspace.read@1")
    contract_version: int = Field(default=1, ge=1, le=1000)


class ProviderBlock(BaseModel):
    """客户端新写法里的 Provider 分组（能力带**真实归属**，以此为准）。"""

    model_config = ConfigDict(extra="allow")

    provider_id: str = Field(..., min_length=1, max_length=160)
    health_status: str = Field(default="", max_length=40)
    dispatchable: bool = True
    plugin_id: str = Field(default="", max_length=160)
    plugin_version: str = Field(default="", max_length=60)
    provider_version: str = Field(default="", max_length=60)
    capabilities: list[CapabilityDeclaration] = Field(default_factory=list, max_length=64)


class RevokedCapability(BaseModel):
    model_config = ConfigDict(extra="allow")

    capability: str = Field(default="", max_length=160)
    provider_id: str = Field(default="", max_length=160)
    reason: str = Field(default="", max_length=120)
    revoked_at: str = Field(default="", max_length=60)


class RevokedProvider(BaseModel):
    model_config = ConfigDict(extra="allow")

    provider_id: str = Field(default="", max_length=160)
    reason: str = Field(default="", max_length=120)
    revoked_at: str = Field(default="", max_length=60)


class RegisterProviderRequest(BaseModel):
    """注册（或覆盖）本机 Provider 的能力租约（与客户端 payload 同构、向后兼容）。"""

    model_config = ConfigDict(extra="allow")

    provider_id: str = Field(default="", max_length=160)
    #: 旧写法：全量扁平能力列表（兼容保留）。新写法用 ``providers``。
    capabilities: list[CapabilityDeclaration] = Field(default_factory=list, max_length=64)
    #: 新写法：按 Provider 分组，能力带真实归属。
    providers: list[ProviderBlock] = Field(default_factory=list, max_length=16)
    #: 客户端主动上报的撤销（本机健康失败/插件卸载），服务端据此摘除对应租约。
    revoked_capabilities: list[RevokedCapability] = Field(default_factory=list, max_length=64)
    revoked_providers: list[RevokedProvider] = Field(default_factory=list, max_length=16)
    device_id: str = Field(default="", max_length=160)
    conversation_id: str = Field(default="", max_length=160)
    workspace_id: str = Field(default="", max_length=160)
    session_id: str = Field(default="", max_length=160)
    user_id: str = Field(default="", max_length=160, description="仅用于核对，服务端以 token 为准")
    deployment: str = Field(default="client", max_length=40)
    trust_level: str = Field(default="official", max_length=40)
    plugin_id: str = Field(default="", max_length=160)
    plugin_version: str = Field(default="", max_length=60)
    provider_version: str = Field(default="", max_length=60)
    scope: dict[str, Any] = Field(default_factory=dict)
    ttl_seconds: float | None = Field(default=None, gt=0, le=3600)
    health_status: str = Field(default="unknown", max_length=40)
    job_id: str = Field(default="", max_length=160, description="可选：把状态事件挂到该任务的流上")


class HeartbeatRequest(BaseModel):
    """租约续期（与客户端 payload 同构；未列出的能力保持不变）。"""

    model_config = ConfigDict(extra="allow")

    provider_id: str = Field(default="", max_length=160)
    capabilities: list[CapabilityDeclaration] = Field(default_factory=list, max_length=64)
    providers: list[ProviderBlock] = Field(default_factory=list, max_length=16)
    revoked_capabilities: list[RevokedCapability] = Field(default_factory=list, max_length=64)
    revoked_providers: list[RevokedProvider] = Field(default_factory=list, max_length=16)
    ttl_seconds: float | None = Field(default=None, gt=0, le=3600)
    health_status: str = Field(default="", max_length=40)
    job_id: str = Field(default="", max_length=160)


class UnregisterProviderRequest(BaseModel):
    """注销（客户端退出/插件停用）。"""

    provider_id: str = Field(..., min_length=1, max_length=160)
    capability: str = Field(default="", max_length=160)
    job_id: str = Field(default="", max_length=160)


class CapabilityInvokeRequest(BaseModel):
    """显式调用一个能力（诊断/客户端回环/后续 Skill 的执行入口）。

    这是 Broker 的**真实调用面**：请求体只表达"要什么、参数是什么"，授权事实
    （工作区/设备/项目/会话）一律由服务端从 token 与 job 解析，不接受客户端自述。
    """

    capability: str = Field(..., min_length=3, max_length=160, description="如 workspace.read@1")
    arguments: dict[str, Any] = Field(default_factory=dict)
    job_id: str = Field(default="", max_length=160)
    workspace_id: str = Field(default="", max_length=160, description="仅用于核对 job 绑定")
    device_id: str = Field(default="", max_length=160)
    conversation_id: str = Field(default="", max_length=160)
    policy_id: str = Field(default="", max_length=80)
    request_id: str = Field(default="", max_length=160)
    idempotency_key: str = Field(default="", max_length=160)
    timeout_seconds: float = Field(default=0.0, ge=0, le=3600)
    preferred_deployment: str = Field(default="", max_length=40)


class CapabilityDenyRequest(BaseModel):
    """客户端本地拒止回传（本机最终否决权）。"""

    capability: str = Field(..., min_length=3, max_length=160)
    provider_id: str = Field(default="", max_length=160)
    reason_code: str = Field(default="other", max_length=60)
    reason: str = Field(default="", max_length=300)
    job_id: str = Field(default="", max_length=160)
    request_id: str = Field(default="", max_length=160)


class AdminRevokeRequest(BaseModel):
    """管理端强制撤销（撤销方不能等 120s 自然过期）。"""

    provider_id: str = Field(default="", max_length=160)
    capability: str = Field(default="", max_length=160, description="留空则撤销该 Provider 的全部能力")
    reason: str = Field(default="admin_revoked", max_length=60)
    job_id: str = Field(default="", max_length=160)


def _leases_for_tenant(user_id: str) -> list[dict[str, Any]]:
    """只列当前用户自己的租约（不能看到别人的设备/工作区绑定）。"""
    rows: list[dict[str, Any]] = []
    for lease in lease_service.snapshot():
        if str(lease.user_id or "") != str(user_id or ""):
            continue
        rows.append(lease.to_snapshot() | {"scope": dict(lease.scope or {})})
    return rows


def _flatten_declarations(req: Any) -> list[dict[str, Any]]:
    """把客户端两种写法统一成"某 Provider 声明了哪些能力"。

    新写法 ``providers[].capabilities`` **带真实归属**，因此优先；旧写法
    ``capabilities[]``（全量扁平）作为兼容兜底。两边都可能有，去重后合并。
    """
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    fallback_provider = str(getattr(req, "provider_id", "") or "")
    fallback_version = str(getattr(req, "provider_version", "") or "")
    fallback_plugin = str(getattr(req, "plugin_id", "") or "")
    fallback_plugin_version = str(getattr(req, "plugin_version", "") or "")

    def add(
        provider_id: str,
        declaration: Any,
        *,
        provider_version: str = "",
        plugin_id: str = "",
        plugin_version: str = "",
        health_status: str = "",
    ) -> None:
        capability = str(getattr(declaration, "capability", "") or "").strip()
        if not capability:
            return
        pid = str(provider_id or fallback_provider or "").strip()
        key = (pid, capability.split("@", 1)[0])
        if key in seen:
            return
        seen.add(key)
        rows.append(
            {
                "provider_id": pid,
                "capability": capability,
                "contract_version": int(getattr(declaration, "contract_version", 1) or 1),
                "provider_version": str(provider_version or fallback_version),
                "plugin_id": str(plugin_id or fallback_plugin),
                "plugin_version": str(plugin_version or fallback_plugin_version),
                "health_status": str(health_status or ""),
            }
        )

    for block in getattr(req, "providers", None) or []:
        if not getattr(block, "dispatchable", True):
            # 客户端明确说"不可派发"：不注册（等价于撤销）。
            continue
        for declaration in getattr(block, "capabilities", None) or []:
            add(
                getattr(block, "provider_id", ""),
                declaration,
                provider_version=getattr(block, "provider_version", ""),
                plugin_id=getattr(block, "plugin_id", ""),
                plugin_version=getattr(block, "plugin_version", ""),
                health_status=getattr(block, "health_status", ""),
            )
    if not rows:
        for declaration in getattr(req, "capabilities", None) or []:
            add(fallback_provider, declaration)
    return rows


def _group_by_provider(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["provider_id"]), []).append(row)
    return grouped


async def _apply_revocations(req: Any, *, user_id: str, job_id: str = "") -> list[dict[str, Any]]:
    """处理客户端上报名单（``revoked_providers`` / ``revoked_capabilities``）。

    撤销是本机权威事实：服务端**立即摘除**对应租约（不等 120s 自然过期），
    这样"客户端已隔离 Provider"与"服务端还能派发"之间的窗口不存在。
    """
    revoked: list[dict[str, Any]] = []
    for item in getattr(req, "revoked_providers", None) or []:
        provider_id = str(getattr(item, "provider_id", "") or "").strip()
        if not provider_id:
            continue
        removed = await lease_service.unregister(
            provider_id=provider_id, user_id=user_id, job_id=job_id
        )
        revoked.extend(
            {"capability": lease.qualified_capability, "provider_id": lease.provider_id,
             "reason": str(getattr(item, "reason", "") or "revoked_provider")}
            for lease in removed
        )
    for item in getattr(req, "revoked_capabilities", None) or []:
        capability = str(getattr(item, "capability", "") or "").strip()
        provider_id = str(getattr(item, "provider_id", "") or "").strip()
        # 必须给 provider_id：只给能力名会跨 Provider 误删（同名能力可能属于多个 Provider）。
        if not capability or not provider_id:
            continue
        removed = await lease_service.unregister(
            provider_id=provider_id, capability=capability, user_id=user_id, job_id=job_id
        )
        revoked.extend(
            {"capability": lease.qualified_capability, "provider_id": lease.provider_id,
             "reason": str(getattr(item, "reason", "") or "revoked_capability")}
            for lease in removed
        )
    return revoked


def _lease_envelope(leases: list[Any], *, revoked: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """给客户端的租约信封。

    ``expires_at`` 语义（客户端最容易踩的点）：返回**未来时间**表示继续续租；不返回
    或返回过去时间／非 2xx 时客户端会停止续租，最迟 120s 后本机调用返回
    ``LEASE_EXPIRED``。因此这里：

    * 始终显式返回最保守（最早）的过期时间，让两边对"这一刻租约还能用多久"理解一致；
    * 本轮**没有续上任何租约**时返回 ``0.0``（过去时间）并把 ``renewed`` 置 False，
      客户端据此明确停止续租——不要让它以为"沉默就是继续"。
    """
    expires = [float(getattr(lease, "expires_at", 0.0) or 0.0) for lease in leases]
    future = [value for value in expires if value > 0]
    return {
        "leases": [lease.to_snapshot() for lease in leases],
        "renewed": bool(leases),
        "expires_at": min(future) if future else 0.0,
        "lease_ttl_seconds": CLIENT_LEASE_TTL_SECONDS,
        "revoked": list(revoked or []),
    }


@router.get("")
async def list_capabilities(payload: dict = Depends(require_auth)):
    """能力目录 + 我的活跃租约（前端"已安装/可用能力"面板的数据源）。"""
    catalog = lease_service.catalog
    return {
        "code": 0,
        "data": {
            "catalog": catalog.to_snapshot(),
            "leases": _leases_for_tenant(payload["sub"]),
            "ladder_note": "local_only 能力必须在客户端注册；cloud 能力不允许注册到客户端",
        },
    }


@router.post("/register")
async def register_capability_provider(
    req: RegisterProviderRequest,
    payload: dict = Depends(require_auth),
):
    """注册 Provider 能力租约（用户主体取自 token）。

    兼容客户端两种写法（``providers[].capabilities`` 优先，``capabilities[]`` 兜底），
    并先处理 ``revoked_*``：撤销是本机权威事实，必须**立即摘除**而不是等 120s 自然过期。
    """
    rows = _flatten_declarations(req)
    if not rows:
        raise BadRequestException(
            "没有可注册的能力声明（providers[].capabilities 或 capabilities 至少给一个）",
            error_code="CAPABILITY_MISSING",
        )
    revoked = await _apply_revocations(req, user_id=payload["sub"], job_id=req.job_id)
    leases: list[Any] = []
    try:
        for provider_id, group in _group_by_provider(rows).items():
            if not provider_id:
                raise LeaseRejected(
                    "INVALID_ARGUMENTS", "能力声明缺少 provider_id（无法确定归属）"
                )
            head = group[0]
            leases.extend(
                await lease_service.register(
                    provider_id=provider_id,
                    capabilities=[
                        {
                            "capability": item["capability"],
                            "contract_version": item["contract_version"],
                        }
                        for item in group
                    ],
                    user_id=payload["sub"],
                    device_id=req.device_id,
                    conversation_id=req.conversation_id,
                    workspace_id=req.workspace_id,
                    session_id=req.session_id,
                    deployment=req.deployment,
                    trust_level=req.trust_level,
                    plugin_id=str(head.get("plugin_id") or req.plugin_id),
                    plugin_version=str(head.get("plugin_version") or req.plugin_version),
                    provider_version=str(head.get("provider_version") or req.provider_version),
                    scope=req.scope,
                    ttl_seconds=clamp_ttl(req.ttl_seconds or CLIENT_LEASE_TTL_SECONDS),
                    health_status=str(head.get("health_status") or req.health_status),
                    job_id=req.job_id,
                )
            )
    except LeaseRejected as exc:
        # 位置/能力/主体类问题都是客户端可修正的 400，不是 500。
        if exc.code == "PERMISSION_DENIED":
            raise ForbiddenException(exc.message, error_code=exc.code) from exc
        raise BadRequestException(exc.message, error_code=exc.code) from exc
    lease_service.sync_registry()
    return {
        "code": 0,
        "data": _lease_envelope(leases, revoked=revoked),
        "message": "能力已注册",
    }


@router.post("/heartbeat")
async def heartbeat_capability_provider(
    req: HeartbeatRequest,
    payload: dict = Depends(require_auth),
):
    """租约续期；已过期必须重新注册（返回 LEASE_EXPIRED）。

    心跳里**不再出现**的能力不会被续期（最迟 120s 后自然过期），客户端也可以用
    ``revoked_*`` 立即摘除。
    """
    revoked = await _apply_revocations(req, user_id=payload["sub"], job_id=req.job_id)
    rows = _flatten_declarations(req)
    grouped = _group_by_provider(rows)
    provider_ids = [pid for pid in grouped if pid] or (
        [req.provider_id] if req.provider_id else []
    )
    if not provider_ids:
        # 只有撤销、没有续期：这是合法心跳（客户端正在摘除能力）。
        lease_service.sync_registry()
        return {
            "code": 0,
            "data": _lease_envelope([], revoked=revoked),
            "message": "已处理撤销" if revoked else "没有需要续期的租约",
        }
    leases: list[Any] = []
    errors: list[dict[str, Any]] = []
    for provider_id in provider_ids:
        group = grouped.get(provider_id) or []
        try:
            leases.extend(
                await lease_service.heartbeat(
                    provider_id=provider_id,
                    user_id=payload["sub"],
                    capabilities=[
                        {"capability": item["capability"], "contract_version": item["contract_version"]}
                        for item in group
                    ],
                    ttl_seconds=clamp_ttl(req.ttl_seconds or CLIENT_LEASE_TTL_SECONDS),
                    health_status=str(
                        (group[0].get("health_status") if group else "") or req.health_status
                    ),
                    job_id=req.job_id,
                )
            )
        except LeaseRejected as exc:
            # 过期必须重新注册：按客户端约定返回**非 2xx**，它会停止续租并在 120s 后
            # 让本机调用返回 LEASE_EXPIRED。这里对多 Provider 逐一处理，不因一个失败
            # 丢掉其它 Provider 的续期结果。
            errors.append({"provider_id": provider_id, "code": exc.code, "message": exc.message})
    # 续期结果为空但客户端确实请求了续期：说明租约已过期（服务端不会靠心跳复活）。
    # 这是客户端需要知道的**结构性结论**，因此显式报 LEASE_EXPIRED。
    if not leases:
        for provider_id in provider_ids:
            for item in grouped.get(provider_id) or []:
                base = str(item["capability"]).split("@", 1)[0]
                if any(
                    lease.provider_id == provider_id and lease.is_expired()
                    for lease in lease_service.leases_for(base)
                ):
                    errors.append(
                        {
                            "provider_id": provider_id,
                            "capability": str(item["capability"]),
                            "code": CapabilityErrorCode.LEASE_EXPIRED.value,
                            "message": "租约已过期，请重新注册能力",
                        }
                    )
                    break
    lease_service.sync_registry()
    if errors and not leases:
        raise BadRequestException(
            "；".join(item["message"] for item in errors[:3]),
            error_code=str(errors[0]["code"]),
        )
    return {
        "code": 0,
        "data": _lease_envelope(leases, revoked=revoked) | {"errors": errors},
        "message": "租约已续期",
    }


@router.post("/unregister")
async def unregister_capability_provider(
    req: UnregisterProviderRequest,
    payload: dict = Depends(require_auth),
):
    """注销 Provider（断开即摘除能力，Broker 不会再选到它）。"""
    removed = await lease_service.unregister(
        provider_id=req.provider_id,
        user_id=payload["sub"],
        capability=req.capability,
        job_id=req.job_id,
    )
    lease_service.sync_registry()
    return {
        "code": 0,
        "data": {"removed": [item.to_snapshot() for item in removed]},
        "message": "已注销" if removed else "没有匹配的租约",
    }


@router.get("/dispatch-map")
async def capability_dispatch_map(payload: dict = Depends(require_auth)):
    """工具 ↔ 能力路由表（客户端 ``capability_bridge.tools`` 的服务端副本）。

    派发**必须按租约**：MCP 原子工具直接调本机实现，不过健康门禁；只按"工具可达"派发
    会让健康隔离与撤销通道失效。这张表是服务端侧的可机读对照，便于两边断言一致。
    """
    from app.agents.capabilities.builtin import TOOL_CAPABILITY_MAP

    return {
        "code": 0,
        "data": {
            "inbound": "mcp",
            "new_endpoint_required": False,
            "tools": [
                {"tool": tool, "capability": capability or None}
                for tool, capability in sorted(TOOL_CAPABILITY_MAP.items())
            ],
            "note": "capability 为 null 表示本机动作（不参与租约）；派发一律按租约+健康门禁",
        },
    }


@router.post("/admin/revoke")
async def admin_revoke_capabilities(
    req: AdminRevokeRequest,
    payload: dict = Depends(require_auth),
):
    """管理端强制撤销（跨 worker 立即生效）。

    撤销方先改 Redis 权威副本（删租约 + 摘索引），再广播失效信号让**所有** worker 清掉
    本地读缓存——只做前者会有窗口期：客户端已隔离，别的 worker 仍按旧缓存派发。
    """
    removed = await lease_service.unregister(
        provider_id=req.provider_id,
        capability=req.capability,
        job_id=req.job_id,
    )
    lease_service.sync_registry()
    from app.services.capability_revoke import broadcast_revoke, normalize_revoke_reason

    reason = normalize_revoke_reason(req.reason or "admin_revoked") if req.reason else "admin_revoked"
    for lease in removed:
        await broadcast_revoke(
            capability=lease.qualified_capability,
            provider_id=lease.provider_id,
            plugin_id=lease.plugin_id,
            lease_id=lease.lease_id,
            reason=reason,
        )
    return {
        "code": 0,
        "data": {
            "revoked": [item.to_snapshot() for item in removed],
            "reason": reason,
        },
        "message": "已撤销并广播" if removed else "没有匹配的租约",
    }


@router.get("/health")
async def capability_health(
    provider_id: str = Query(default="", max_length=160),
    payload: dict = Depends(require_auth),
):
    """Provider 健康与租约过期时间（客户端心跳失败时用它判断是否需要重连）。"""
    rows = [
        row
        for row in _leases_for_tenant(payload["sub"])
        if not provider_id or row.get("provider_id") == provider_id
    ]
    dropped = lease_service.sync_registry()
    return {
        "code": 0,
        "data": {"leases": rows, "dropped_providers": dropped},
    }


async def _resolve_job_binding(job_id: str, user_id: str) -> dict[str, Any]:
    """从任务解析**服务端授权事实**（工作区/项目/会话）；不信任请求体自述。"""
    if not job_id:
        return {}
    try:
        from app.agents.orchestration.orchestrator import orchestrator

        job = await orchestrator.get_job(job_id)
    except Exception:  # noqa: BLE001 - 取不到就按"无绑定"处理（能力会因此不可用）
        return {}
    if job is None or str(getattr(job, "user_id", "")) != str(user_id or ""):
        return {}
    routing = job.routing if isinstance(job.routing, dict) else {}
    return {
        "workspace_id": str(routing.get("workspace_id") or ""),
        "conversation_id": str(getattr(job, "conversation_id", "") or ""),
        "authorized_project_ids": tuple(
            str(item) for item in (routing.get("authorized_project_ids") or [])
        ),
        "policy_id": str(routing.get("policy_id") or ""),
    }


@router.post("/invoke")
async def invoke_capability(
    req: CapabilityInvokeRequest,
    payload: dict = Depends(require_auth),
):
    """经 Broker 调用一个能力（**唯一执行面**）。

    授权事实来自 token + job（请求体里的 workspace/device 只用于核对）；缺 Provider、
    缺审批、被策略拒绝都会返回**结构化错误码**而不是 500——客户端据此决定是
    "重连设备""去审批"还是"改本地策略"。
    """
    from lumi_contracts.plugins import CapabilityInvocation, SessionBinding

    from app.agents.capabilities.broker import capability_broker
    from app.agents.capabilities.context import AgentExecutionContext

    binding = await _resolve_job_binding(req.job_id, payload["sub"])
    workspace_id = str(binding.get("workspace_id") or req.workspace_id or "")
    conversation_id = str(binding.get("conversation_id") or req.conversation_id or "")
    context = AgentExecutionContext.from_metadata(
        user_id=payload["sub"],
        user_role=str(payload.get("role") or "user"),
        conversation_id=conversation_id,
        workspace_id=workspace_id,
        device_id=req.device_id,
        project_ids=binding.get("authorized_project_ids") or (),
    )
    invocation = CapabilityInvocation(
        capability=req.capability,
        arguments=dict(req.arguments or {}),
        request_id=req.request_id,
        idempotency_key=req.idempotency_key,
        timeout_seconds=req.timeout_seconds,
        job_id=req.job_id,
        session_binding=SessionBinding(
            user_id=payload["sub"],
            conversation_id=conversation_id,
            workspace_id=workspace_id,
            device_id=req.device_id,
        ),
    )
    result = await capability_broker.invoke(
        invocation,
        context=context,
        preferred_deployment=req.preferred_deployment,
        job_id=req.job_id,
        policy_id=req.policy_id or str(binding.get("policy_id") or ""),
    )
    return {
        "code": 0,
        "data": {
            "result": result.to_wire(),
            "ok": result.ok,
            "error_code": result.error_code,
            "retryable": result.retryable,
            "needs_install": result.needs_install,
        },
    }


@router.post("/deny")
async def report_local_denial(
    req: CapabilityDenyRequest,
    payload: dict = Depends(require_auth),
):
    """客户端回传**本地拒止**（本机最终否决权）。

    服务端不会把它降级成"换个 Provider 再试"：只记录结构化拒止结果并发能力状态事件，
    前端据此显示"已被本机策略拒绝"。
    """
    from lumi_contracts.plugins import CapabilityInvocation

    from app.agents.capabilities.audit import to_capability_result
    from app.services.capability_events import events_for_result, publish_capability_event

    invocation = CapabilityInvocation(
        capability=req.capability,
        request_id=req.request_id,
        idempotency_key=req.request_id,
    )
    result = to_capability_result(
        invocation,
        provider_id=req.provider_id,
        reason_code=req.reason_code,
        reason=req.reason,
    )
    event = events_for_result(result, capability=invocation.qualified_capability, job_id=req.job_id)
    await publish_capability_event(
        str(event.get("type") or "capability_failed"),
        job_id=req.job_id,
        **{
            key: value
            for key, value in event.items()
            if key not in {"type", "job_id", "occurred_at"}
        },
    )
    return {"code": 0, "data": {"result": result.to_wire()}, "message": "已记录本地拒止"}
