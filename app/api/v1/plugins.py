"""阶段 4/8 REST：插件生命周期（安装/启用/停用/升级/回滚/卸载/健康/扩展类型）。

三件事在这里落地：

* **安装界面需要的事实**：安装列表返回 ``data_leaves_device`` / ``needs_restart`` /
  ``needs_workspace_binding`` / ``needs_local_confirmation`` / 依赖报告 / 验签原因——
  前端据此区分"服务端插件 vs 客户端插件""要不要重启 Electron"；
* **错误是稳定的 400**：类型未登记、签名不符、依赖未满足都带 ``error_code``，
  不是 500，也不是"静默失败"；
* **扩展类型登记**（阶段 8）：登记也只在开发者模式放行，生产默认拒绝。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.deps import require_auth
from app.core.exceptions import BadRequestException, ForbiddenException, NotFoundException
from app.services.plugins import (
    PluginManager,
    PluginRegistry,
    PluginRejected,
    PluginStateStore,
    extension_handlers,
    set_plugin_manager,
)
from lumi_contracts.plugins import IsolationLevel

router = APIRouter()

#: 进程内插件登记服务（状态落 ``PLUGIN_STATE_DIR``；不依赖数据库）。
plugin_registry = PluginRegistry(store=PluginStateStore())

#: 生命周期唯一入口（阶段 4）。开关 ``PLUGIN_QUOTA_ENFORCEMENT`` 关闭时它是注册表的
#: 纯委派——行为、返回值与落盘记录与改造前逐字节一致。
plugin_manager = PluginManager(registry=plugin_registry)
set_plugin_manager(plugin_manager)


class PluginInstallRequest(BaseModel):
    """安装/升级：直接提交 Manifest（内置/官方插件由服务端分发时同样走这条）。"""

    manifest: dict[str, Any] = Field(..., description="PluginManifest（见契约，未知 kind 会被拒绝）")
    activate: bool = Field(default=True, description="安装后是否立即启用")
    upgrade: bool = Field(default=False, description="true=按升级处理（保留回滚点）")


class PluginToggleRequest(BaseModel):
    reason: str = Field(default="", max_length=200)


class PluginRollbackRequest(BaseModel):
    version: str = Field(default="", max_length=60, description="留空则回到 previous_version")


class ExtensionRegisterRequest(BaseModel):
    kind: str = Field(..., min_length=5, max_length=80, description="形如 ext.custom")
    handler_id: str = Field(..., min_length=1, max_length=160)
    isolation: str = Field(default="sandboxed", max_length=40)
    reason: str = Field(default="", max_length=300)
    audited: bool = Field(default=False)


def _developer_mode() -> bool:
    try:
        from app.core.config import settings

        return bool(getattr(settings, "PLUGIN_DEVELOPER_MODE", False))
    except Exception:  # noqa: BLE001 - 配置不可用时按生产处理
        return False


def _reject(exc: PluginRejected) -> Exception:
    """插件拒绝 → 稳定 HTTP 错误（权限类给 403，其余 400）。"""
    if exc.code in {"PLUGIN_KIND_REQUIRES_DEVELOPER_MODE", "PLUGIN_DEPLOYMENT_NOT_ALLOWED"}:
        return ForbiddenException(exc.message, error_code=exc.code)
    return BadRequestException(exc.message, error_code=exc.code)


@router.get("")
async def list_plugins(payload: dict = Depends(require_auth)):
    """已安装插件 + 可用能力/策略 + 扩展类型（"已安装/可用能力"面板的数据源）。

    每个插件额外带 ``quota_status``：declared / observed / wired / enforced 四级，
    诚实反映"配额到底执行到哪一步"（协作式不算 enforced）。
    """
    statuses = {item["plugin_id"]: item for item in plugin_manager.quota_statuses()}
    plugins: list[dict[str, Any]] = []
    for item in plugin_manager.all():
        payload_item = item.to_api()
        payload_item["quota_status"] = statuses.get(item.plugin_id, {})
        plugins.append(payload_item)
    return {
        "code": 0,
        "data": {
            "plugins": plugins,
            "developer_mode": _developer_mode(),
            "extension_kinds": extension_handlers.to_snapshot(),
            "known_kinds": [
                "skill_plugin",
                "capability_provider",
                "policy_pack",
                "view_plugin",
                "extension_handler",
            ],
        },
    }


@router.post("")
async def install_plugin(req: PluginInstallRequest, payload: dict = Depends(require_auth)):
    """安装（或升级）插件：Manifest → 验签 → 依赖 → 位置/隔离 → 健康 → 激活。"""
    try:
        manifest = PluginRegistry.parse_manifest(req.manifest)
        existing = plugin_manager.get(manifest.id)
        if req.upgrade and existing is not None:
            installation = await plugin_manager.upgrade(manifest)
        else:
            installation = await plugin_manager.install(manifest, activate=req.activate)
    except PluginRejected as exc:
        raise _reject(exc) from exc
    return {"code": 0, "data": installation.to_api(), "message": "插件已安装"}


@router.get("/{plugin_id}")
async def get_plugin(plugin_id: str, payload: dict = Depends(require_auth)):
    installation = plugin_manager.get(plugin_id)
    if installation is None:
        raise NotFoundException("插件未安装", error_code="PLUGIN_NOT_INSTALLED")
    data = installation.to_api()
    data["quota_status"] = plugin_manager.quota_status(plugin_id)
    return {"code": 0, "data": data}


@router.get("/{plugin_id}/quota")
async def plugin_quota_status(plugin_id: str, payload: dict = Depends(require_auth)):
    """该插件的配额**诚实**状态（结构化四级：declared / observed / wired / enforced）。

    ``enforced`` 只在"开关打开 + 硬约束 + 真走过可强杀进程"时为真；
    只走过进程内协作取消会给出 ``cooperative_only=true`` 与 ``not_enforced_because``。
    """
    if plugin_manager.get(plugin_id) is None:
        raise NotFoundException("插件未安装", error_code="PLUGIN_NOT_INSTALLED")
    return {"code": 0, "data": plugin_manager.quota_status(plugin_id)}


@router.post("/{plugin_id}/enable")
async def enable_plugin(plugin_id: str, payload: dict = Depends(require_auth)):
    try:
        installation = await plugin_manager.enable(plugin_id)
    except PluginRejected as exc:
        if exc.code == "PLUGIN_NOT_INSTALLED":
            raise NotFoundException(exc.message, error_code=exc.code) from exc
        raise _reject(exc) from exc
    return {"code": 0, "data": installation.to_api(), "message": "插件已启用"}


@router.post("/{plugin_id}/disable")
async def disable_plugin(
    plugin_id: str,
    req: PluginToggleRequest | None = None,
    payload: dict = Depends(require_auth),
):
    try:
        installation = await plugin_manager.disable(plugin_id, reason=(req.reason if req else ""))
    except PluginRejected as exc:
        if exc.code == "PLUGIN_NOT_INSTALLED":
            raise NotFoundException(exc.message, error_code=exc.code) from exc
        raise _reject(exc) from exc
    return {"code": 0, "data": installation.to_api(), "message": "插件已停用"}


@router.post("/{plugin_id}/rollback")
async def rollback_plugin(
    plugin_id: str,
    req: PluginRollbackRequest | None = None,
    payload: dict = Depends(require_auth),
):
    """回滚到上一版本；回滚后插件处于**停用**状态，需要重新启用（重新过门禁）。"""
    try:
        installation = plugin_manager.rollback(plugin_id, version=(req.version if req else ""))
    except PluginRejected as exc:
        if exc.code == "PLUGIN_NOT_INSTALLED":
            raise NotFoundException(exc.message, error_code=exc.code) from exc
        raise _reject(exc) from exc
    return {"code": 0, "data": installation.to_api(), "message": "已回滚（需重新启用）"}


@router.get("/{plugin_id}/health")
async def plugin_health(plugin_id: str, payload: dict = Depends(require_auth)):
    try:
        return {"code": 0, "data": plugin_manager.health(plugin_id)}
    except PluginRejected as exc:
        raise NotFoundException(exc.message, error_code=exc.code) from exc


@router.delete("/{plugin_id}")
async def uninstall_plugin(plugin_id: str, payload: dict = Depends(require_auth)):
    try:
        await plugin_manager.uninstall(plugin_id)
    except PluginRejected as exc:
        if exc.code == "PLUGIN_NOT_INSTALLED":
            raise NotFoundException(exc.message, error_code=exc.code) from exc
        raise _reject(exc) from exc
    return {"code": 0, "message": "插件已卸载"}


# ── 阶段 8：扩展类型登记 ─────────────────────────────────────────


@router.get("/extension-handlers")
async def list_extension_handlers(payload: dict = Depends(require_auth)):
    return {
        "code": 0,
        "data": {
            "handlers": extension_handlers.to_snapshot(),
            "developer_mode": _developer_mode(),
            "note": "未知 kind 默认拒绝；登记扩展类型也需要开发者模式才能激活",
        },
    }


@router.post("/extension-handlers")
async def register_extension_handler(
    req: ExtensionRegisterRequest,
    payload: dict = Depends(require_auth),
):
    """登记扩展类型（生产环境默认拒绝：只有开发者模式才允许）。"""
    if not _developer_mode():
        raise ForbiddenException(
            "扩展类型登记只在开发者模式可用（生产默认拒绝未注册的插件类型）",
            error_code="PLUGIN_KIND_REQUIRES_DEVELOPER_MODE",
        )
    try:
        handler = extension_handlers.register(
            kind=req.kind,
            handler_id=req.handler_id,
            isolation=IsolationLevel(req.isolation),
            reason=req.reason,
            audited=req.audited,
        )
    except (ValueError, KeyError) as exc:
        raise BadRequestException(str(exc), error_code="EXTENSION_KIND_INVALID") from exc
    return {"code": 0, "data": handler.to_dict(), "message": "扩展类型已登记"}


__all__ = [
    "plugin_manager",
    "plugin_registry",
    "router",
]
