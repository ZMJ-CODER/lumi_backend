"""管理员运维面板 API：运行时策略热更新（Redis Key + TTL + 本地轮询）。

## 为什么是"简易面板"而不是配置中心

运维真正需要的动作只有四个：**看现状、改一条、删一条、确认所有 Worker 都换过来了**。
为此引入 ConfigServer 是过度设计——策略放 Redis（``policy:epoch`` + ``policy:<epoch>``）、
Worker 每 10 秒轮询一次 epoch，就够了。

## 权限

写操作必须 **superadmin JWT + ``X-Admin-Token``**（与 ``PUT /admin/llm-config/models``
同一套：``_require_admin_verified``）。这是本模块**刻意**不沿用
``POST /capabilities/admin/revoke`` 的原因——那个端点只校验 ``require_auth``，
任何登录用户都能凭 provider_id 撤销别人的租约（越权面），不能作为新接口的模板。

## 生效范围与延迟

* 写完后**本进程**立即生效（``PolicyStore.put`` 会把新桶装进本地缓存）；
* 其它 Worker 最迟一个轮询间隔（默认 10s）后生效——这是"用轮询代替配置中心"的代价；
* ``RUNTIME_POLICY_OVERRIDE`` 关闭时，接口仍然可写（管理动作不依赖开关），但不会
  有任何 Worker 读它：``GET /policies`` 的 ``enabled`` 字段会如实返回 ``false``。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from loguru import logger

from app.core.deps import get_admin_verified_token, require_superadmin
from app.core.exceptions import BadRequestException, ConflictException
from app.services.runtime_policy import (
    SCOPE_DEFAULT,
    SCOPE_MODEL,
    SCOPE_PROVIDER,
    PolicyStore,
    RuntimePolicy,
    default_max_ttl_seconds,
    policy_bucket_ttl_seconds,
    policy_store,
)
from app.services.write_gate import WRITE_SCOPE, write_gate

#: 路由前缀约定：与 ``admin_system`` 一致——router 不带前缀，
#: 由 ``app/api/router.py`` 以 ``prefix="/admin/policies"`` 挂载。
router = APIRouter(tags=["admin-policies"])

#: 允许的策略主体（与前端下拉一致；未知值直接 400，不做静默兜底）。
VALID_SCOPES: frozenset[str] = frozenset({SCOPE_PROVIDER, SCOPE_MODEL, SCOPE_DEFAULT})

#: 上限保护：超时/并发/可信时间都必须落在合理区间，否则一条手滑的配置就能让线上
#: 全部 LLM 调用瞬间超时（``timeout_seconds=0.001``）或把并发压到 1。
MIN_TIMEOUT_SECONDS = 0.1
MAX_TIMEOUT_SECONDS = 3600.0
MAX_CONCURRENT_LIMIT = 256
MAX_TTL_LIMIT = 86400.0


def _require_admin_verified(x_admin_token: str | None, payload: dict) -> None:
    """与 ``app/api/v1/admin.py`` 同一实现（延迟导入避免循环）。"""
    from app.api.v1.admin import _require_admin_verified as impl

    impl(x_admin_token, payload)


def _store() -> PolicyStore:
    return policy_store


def _publish_error(exc: Exception) -> BadRequestException:
    """把发布失败翻成结构化 4xx：**冲突必须是 409**，不能和"Redis 挂了"混成 400。

    客户端对两者的动作完全不同：409 表示"有人同时在改，请刷新面板再提交"（可重试且
    必须重建请求），Redis 不可用则要去看基础设施。混成一个码会让运维反复重试同一份
    过期改动，而每次重试都可能覆盖别人的策略。
    """
    from app.services.runtime_policy import PolicyPublishConflict

    if isinstance(exc, PolicyPublishConflict):
        return ConflictException(str(exc), error_code="POLICY_PUBLISH_CONFLICT")
    return BadRequestException(str(exc), error_code="POLICY_STORE_UNAVAILABLE")


def _field_for(scope: str, target: str) -> str:
    name = str(scope or "").strip().lower()
    if name not in VALID_SCOPES:
        raise BadRequestException(
            f"未知策略主体：{scope!r}（只能是 {sorted(VALID_SCOPES)}）", error_code="INVALID_SCOPE"
        )
    subject = str(target or "").strip()
    if name != SCOPE_DEFAULT and not subject:
        raise BadRequestException("非 default 策略必须给出 target（provider_id 或模型名）", error_code="INVALID_TARGET")
    policy = RuntimePolicy(scope=name, target=subject)
    return policy.field_name


def _validate_number(value: Any, *, name: str, minimum: float, maximum: float) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise BadRequestException(f"{name} 必须是数字，收到 {value!r}", error_code="INVALID_ARGUMENTS") from None
    if number < minimum or number > maximum:
        raise BadRequestException(
            f"{name} 必须在 [{minimum}, {maximum}] 之间，收到 {number}", error_code="INVALID_ARGUMENTS"
        )
    return number


def _surface_view(entries: list) -> dict:
    """模型可见面收敛视图（Phase 5，只读）：

    * ``model_facing_tools``：收敛后模型会看到的名字；
    * ``hidden_tools``：打开开关后会从可见面消失的具体工具名；
    * ``entries``：逐工具去向（``hidden`` / ``kept``）——**分类不了的工具有意保留**。
    """
    try:
        from app.agents.capabilities.resource_surface import (
            hidden_tools,
            surface_entries,
            surface_snapshot,
        )

        return {
            **surface_snapshot(),
            "hidden_tools": hidden_tools(entries),
            "entries": [entry.as_dict() for entry in surface_entries(entries)],
        }
    except Exception as exc:  # noqa: BLE001 - 展示失败不能影响注册表视图
        logger.debug("[admin] 模型可见面视图失败: {}", str(exc)[:120])
        return {}


@router.get("/tools")
async def tool_registry_view(payload: dict = Depends(require_superadmin)):
    """工具注册表：条目 + **静态/派生影子差异**（切换真相源前的证据）。

    只读。差异为空的维度才可以安全地把真相源切到注册表；有差异时先修派生逻辑，
    不要急着打开 ``TOOL_REGISTRY_DERIVED``——差异意味着"打开后行为会变"。
    """
    from app.agents.capabilities.tool_registry import (
        SHADOW_DECLARED_DIMENSIONS,
        SHADOW_PARITY_DIMENSIONS,
        build_registry_entries,
        registry_derived_enabled,
        shadow_compare,
        shadow_parity_totals,
    )
    from app.core.config import settings

    entries = build_registry_entries()
    diffs = shadow_compare()
    totals = shadow_parity_totals(diffs)
    # 统一资源能力层（Phase 1）：**只读进度**——哪些工具已经有 (统一能力, 资源类型, Provider)
    # 元数据，哪些还没有。它能回答"迁移到哪一步了"，而不是靠翻代码数。
    from app.agents.capabilities.resource_catalog import (
        catalog_snapshot,
        unbound_tools,
    )

    bound = [entry for entry in entries if entry.unified_capability]
    unbound = unbound_tools(entry.name for entry in entries)
    return {
        "code": 0,
        "data": {
            "derived_enabled": registry_derived_enabled(),
            "flag_default": bool(getattr(settings, "TOOL_REGISTRY_DERIVED", False)),
            "count": len(entries),
            "entries": [entry.as_dict() for entry in entries],
            "shadow_diff": diffs,
            # 维度清单**由后端给**：前端不要自己维护一份硬编码列表——新增一个判定/
            # 披露维度时，硬编码列表会把新维度静默丢掉（本仓库真实发生过一次）。
            "shadow_dimensions": {
                "parity": list(SHADOW_PARITY_DIMENSIONS),
                "declared": list(SHADOW_DECLARED_DIMENSIONS),
                "missing": list(totals["missing_dimensions"]),
            },
            "shadow_diff_total": totals["parity_total"],
            # 声明档位带来的差异是**有意**的（静态词表表达不了声明），单独披露。
            "shadow_declared_total": totals["declared_total"],
            # 维度没算成 ≠ 一致：缺失维度一律视为不可切换。
            "shadow_missing_dimensions": totals["missing_dimensions"],
            "switch_safe": totals["switch_safe"],
            # ── 统一资源能力层（Phase 1，只读）──
            "resource_catalog": catalog_snapshot(),
            "resource_bound_count": len(bound),
            "resource_unbound_tools": unbound,
            # Phase 5：模型可见面收敛的**词表与去向**（打开开关后哪些名字会消失）。
            "resource_surface": _surface_view(entries),
        },
        "message": (
            "影子差异为空，可安全切换真相源"
            if totals["switch_safe"]
            else "存在差异或维度未算成，暂不建议切换真相源"
        ),
    }


@router.get("")
async def list_policies(payload: dict = Depends(require_superadmin)):
    """查看当前策略、本地缓存新鲜度与轮询状态（只读，不需要二次验证）。"""
    snapshot = _store().snapshot()
    snapshot["write_gate"] = write_gate.snapshot()
    snapshot["bucket_ttl_seconds"] = policy_bucket_ttl_seconds()
    snapshot["default_max_ttl_seconds"] = default_max_ttl_seconds()
    return {"code": 0, "data": snapshot, "message": "当前运行时策略"}


@router.put("")
async def upsert_policy(
    req: dict,
    payload: dict = Depends(require_superadmin),
    x_admin_token: str | None = Depends(get_admin_verified_token),
):
    """写入/覆盖一条策略并推进 epoch。

    ``target`` 可以是 ``provider_id``（如 ``lumi.local.workspace``）或模型名
    （如 ``deepseek-v4-flash``）；``scope=default`` 时不填。未给的字段不覆盖，
    因此"只想改超时"不会顺手把并发或启停改掉。
    """
    _require_admin_verified(x_admin_token, payload)
    scope = str(req.get("scope") or SCOPE_PROVIDER).strip().lower()
    field_name = _field_for(scope, str(req.get("target") or ""))
    timeout = _validate_number(
        req.get("timeout_seconds"), name="timeout_seconds", minimum=MIN_TIMEOUT_SECONDS, maximum=MAX_TIMEOUT_SECONDS
    )
    max_concurrent_raw = _validate_number(
        req.get("max_concurrent"), name="max_concurrent", minimum=1, maximum=MAX_CONCURRENT_LIMIT
    )
    max_ttl = _validate_number(
        req.get("max_ttl_seconds"), name="max_ttl_seconds", minimum=1.0, maximum=MAX_TTL_LIMIT
    )
    enabled = req.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        text = str(enabled).strip().casefold()
        if text in {"1", "true", "yes", "on"}:
            enabled = True
        elif text in {"0", "false", "no", "off"}:
            enabled = False
        else:
            raise BadRequestException("enabled 必须是布尔值", error_code="INVALID_ARGUMENTS")

    from datetime import datetime, timezone

    policy = RuntimePolicy(
        scope=scope,
        target=str(req.get("target") or "").strip(),
        timeout_seconds=timeout,
        max_concurrent=int(max_concurrent_raw) if max_concurrent_raw else None,
        enabled=enabled,
        max_ttl_seconds=max_ttl,
        note=str(req.get("note") or "")[:200],
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    try:
        epoch = await _store().put(policy)
    except RuntimeError as exc:  # Redis 不可用 / 发布冲突
        raise _publish_error(exc) from exc
    logger.info("[policy] 管理员更新策略 field={} epoch={}", field_name, epoch)
    return {
        "code": 0,
        "data": {"field": field_name, "epoch": epoch, "policy": policy.to_payload()},
        "message": "策略已生效（本进程立即；其它 Worker 最迟一个轮询间隔）",
    }


@router.delete("/{field_name:path}")
async def delete_policy(
    field_name: str,
    payload: dict = Depends(require_superadmin),
    x_admin_token: str | None = Depends(get_admin_verified_token),
):
    """删掉某条覆盖，回落到 ``.env`` / 代码默认值（同样推进 epoch）。"""
    _require_admin_verified(x_admin_token, payload)
    target = str(field_name or "").strip()
    if not target:
        raise BadRequestException("缺少策略 field", error_code="INVALID_ARGUMENTS")
    try:
        epoch = await _store().delete(target)
    except RuntimeError as exc:
        raise _publish_error(exc) from exc
    logger.info("[policy] 管理员删除策略 field={} epoch={}", target, epoch)
    return {"code": 0, "data": {"field": target, "epoch": epoch}, "message": "策略已删除"}


@router.post("/refresh")
async def refresh_policies(
    payload: dict = Depends(require_superadmin),
    x_admin_token: str | None = Depends(get_admin_verified_token),
):
    """立刻轮询一次（排障用：验证"改完是不是所有 Worker 都换过来了"）。"""
    _require_admin_verified(x_admin_token, payload)
    refreshed = await _store().refresh_once()
    return {
        "code": 0,
        "data": {"refreshed": bool(refreshed), **_store().snapshot()},
        "message": "已刷新" if refreshed else "epoch 未变化（本地缓存仍可信）",
    }


@router.post("/write-lease")
async def grant_write_lease(
    req: dict,
    payload: dict = Depends(require_superadmin),
    x_admin_token: str | None = Depends(get_admin_verified_token),
):
    """续签写租约（``WRITE_GATE_ENFORCEMENT`` 打开后，写操作需要它）。

    没有走"自动续签"是因为写闸的语义是**授权**：谁有权持续写，得由运维显式给出，
    而不是让写路径自己给自己发租约（那就等于没有闸）。
    """
    _require_admin_verified(x_admin_token, payload)
    scope = str(req.get("scope") or WRITE_SCOPE).strip() or WRITE_SCOPE
    lease_ttl = req.get("lease_ttl_seconds")
    ttl = int(lease_ttl) if lease_ttl not in (None, "") else None
    from app.services.write_gate import WriteGateDenied

    try:
        lease = await write_gate.grant_async(scope=scope, lease_ttl=ttl, source="admin")
    except WriteGateDenied as exc:
        raise BadRequestException(str(exc), error_code=exc.reason) from exc
    return {"code": 0, "data": {"lease": lease.as_dict()}, "message": "写租约已续签"}


__all__ = [
    "MAX_CONCURRENT_LIMIT",
    "MAX_TIMEOUT_SECONDS",
    "MIN_TIMEOUT_SECONDS",
    "VALID_SCOPES",
    "router",
]

