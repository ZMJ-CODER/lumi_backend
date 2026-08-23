"""用户级外部 MCP 工具绑定与能力映射。

MCP Server 配置回答“后端可以连谁”；本模块回答“当前用户明确批准了
哪一个工具、在哪个场景、以什么确认策略进入模型候选集”。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.agents.skills.capability import ToolCapability, role_allows
from app.core.config import settings
from app.core.database import async_session_factory
from app.core.redis import get_redis
from app.models.db_models import UserMcpToolBinding

_active_binding_tasks: dict[str, dict[str, int]] = {}


def _configured_external_server(server_name: str) -> dict | None:
    for server in settings.MCP_SERVERS or []:
        if str(server.get("name") or "") == server_name and bool(server.get("allow_user_binding", False)):
            return server
    return None


def _configured_quota(server: dict) -> tuple[int, int]:
    """Return bounded per-binding limits from deployment-owned configuration."""
    daily = int(server.get("mcp_daily_call_limit", settings.MCP_EXTERNAL_DEFAULT_DAILY_CALL_LIMIT))
    concurrency = int(server.get("mcp_concurrency_limit", settings.MCP_EXTERNAL_DEFAULT_CONCURRENCY_LIMIT))
    return max(1, daily), max(1, concurrency)


def configured_external_server_names() -> set[str]:
    return {
        str(server.get("name"))
        for server in settings.MCP_SERVERS or []
        if server.get("name") and bool(server.get("allow_user_binding", False))
    }


def _fingerprint(server_name: str, raw_tool_name: str, schema: dict) -> str:
    value = json.dumps(
        {"server": server_name, "tool": raw_tool_name, "schema": schema},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def discover_bindable_tools(server_name: str) -> list[dict]:
    """Discover only deployment-approved external servers, never arbitrary URLs."""
    if _configured_external_server(server_name) is None:
        return []
    from app.agents.mcp.manager import list_tools

    return await list_tools(server_name)


async def create_binding(user_id: str, data) -> UserMcpToolBinding:
    server = _configured_external_server(data.server_name)
    if server is None:
        raise ValueError("MCP Server 未配置为允许用户绑定")
    tools = await discover_bindable_tools(data.server_name)
    remote = next((item for item in tools if str(item.get("name")) == data.raw_tool_name), None)
    if remote is None:
        raise ValueError("MCP Server 未声明该工具，不能创建绑定")

    schema = remote.get("input_schema") if isinstance(remote.get("input_schema"), dict) else {"type": "object", "properties": {}}
    # 平台策略只允许收紧远端声明，不能因用户提交而降低外部工具风险。
    remote_write = bool(remote.get("write_op", True))
    write_op = remote_write if data.write_op is None else bool(data.write_op or remote_write)
    # 外部 MCP 的 annotation 只是远端自述，不能被用来降低平台的确认策略。
    # 第一版统一走服务端指纹确认；受控 Electron 本地 Skill 仍沿用既有 client
    # 确认通道，不走本绑定表。
    confirmation_mode = "server"
    requires_confirmation = True
    remote_idempotent = bool(remote.get("idempotent", False))
    # 重试语义同样不能由用户请求体或远端自述放宽。只有远端明确声称幂等、且
    # 映射不是写操作时才允许自动重试；用户只能进一步收紧为不可重试。
    idempotent = remote_idempotent and not write_op
    if data.idempotent is False:
        idempotent = False
    daily_call_limit, concurrency_limit = _configured_quota(server)
    requires_admin_review = bool(
        server.get("mcp_require_admin_approval", settings.MCP_EXTERNAL_REQUIRE_ADMIN_APPROVAL)
    )

    values = {
        "id": uuid.uuid4(),
        "user_id": uuid.UUID(str(user_id)),
        "server_name": data.server_name,
        "raw_tool_name": data.raw_tool_name,
        "display_name": str(remote.get("name") or data.raw_tool_name),
        "description": str(remote.get("description") or ""),
        "input_schema": schema,
        "domain": data.domain,
        "intent_tags": list(data.intent_tags),
        "scenes": list(data.scenes),
        "permission": data.permission,
        "write_op": write_op,
        "requires_confirmation": requires_confirmation,
        "confirmation_mode": confirmation_mode,
        "idempotent": idempotent,
        "resource_templates": list(remote.get("resource_templates") or []),
        "daily_call_limit": daily_call_limit,
        "concurrency_limit": concurrency_limit,
        "status": "pending_approval" if requires_admin_review else "enabled",
    }
    async with async_session_factory() as session:
        stmt = insert(UserMcpToolBinding).values(**values).on_conflict_do_update(
            constraint="uq_user_mcp_tool_binding",
            set_={key: value for key, value in values.items() if key not in {"user_id", "server_name", "raw_tool_name"}},
        ).returning(UserMcpToolBinding)
        binding = (await session.execute(stmt)).scalar_one()
        await session.commit()
        from app.agents.mcp.manager import invalidate_tool_cache

        invalidate_tool_cache(data.server_name)
        return binding


async def list_bindings(user_id: str) -> list[UserMcpToolBinding]:
    async with async_session_factory() as session:
        result = await session.execute(
            select(UserMcpToolBinding).where(UserMcpToolBinding.user_id == uuid.UUID(str(user_id))).order_by(UserMcpToolBinding.created_at.desc())
        )
        return list(result.scalars())


async def set_binding_enabled(user_id: str, binding_id: str, enabled: bool) -> bool:
    async with async_session_factory() as session:
        binding = await session.get(UserMcpToolBinding, uuid.UUID(str(binding_id)))
        if binding is None or str(binding.user_id) != str(user_id):
            return False
        # 用户不能用“启用”接口绕过部署方要求的管理员审核。
        if enabled and binding.status == "pending_approval":
            return False
        binding.status = "enabled" if enabled else "revoked"
        await session.commit()
        from app.agents.mcp.manager import invalidate_tool_cache

        invalidate_tool_cache(binding.server_name)
        if not enabled:
            await cancel_active_binding_calls(binding_id)
        return True


async def delete_binding(user_id: str, binding_id: str) -> bool:
    async with async_session_factory() as session:
        binding = await session.get(UserMcpToolBinding, uuid.UUID(str(binding_id)))
        if binding is None or str(binding.user_id) != str(user_id):
            return False
        server_name = binding.server_name
        await session.delete(binding)
        await session.commit()
        from app.agents.mcp.manager import invalidate_tool_cache

        invalidate_tool_cache(server_name)
        await cancel_active_binding_calls(binding_id)
        return True


async def list_pending_bindings() -> list[UserMcpToolBinding]:
    async with async_session_factory() as session:
        result = await session.execute(
            select(UserMcpToolBinding)
            .where(UserMcpToolBinding.status == "pending_approval")
            .order_by(UserMcpToolBinding.created_at.asc())
        )
        return list(result.scalars())


async def review_binding(binding_id: str, approved: bool) -> UserMcpToolBinding | None:
    try:
        bid = uuid.UUID(str(binding_id))
    except (TypeError, ValueError):
        return None
    async with async_session_factory() as session:
        binding = await session.get(UserMcpToolBinding, bid)
        if binding is None or binding.status != "pending_approval":
            return None
        binding.status = "enabled" if approved else "revoked"
        server_name = binding.server_name
        await session.commit()
    from app.agents.mcp.manager import invalidate_tool_cache

    invalidate_tool_cache(server_name)
    if not approved:
        await cancel_active_binding_calls(binding_id)
    return binding


def register_active_binding_call(binding_id: str, task_id: str) -> None:
    if not binding_id or not task_id:
        return
    tasks = _active_binding_tasks.setdefault(binding_id, {})
    tasks[task_id] = tasks.get(task_id, 0) + 1


def unregister_active_binding_call(binding_id: str, task_id: str) -> None:
    tasks = _active_binding_tasks.get(binding_id)
    if not tasks or not task_id:
        return
    remaining = tasks.get(task_id, 0) - 1
    if remaining > 0:
        tasks[task_id] = remaining
    else:
        tasks.pop(task_id, None)
    if not tasks:
        _active_binding_tasks.pop(binding_id, None)


async def cancel_active_binding_calls(binding_id: str) -> int:
    """Best-effort cancellation for calls visible to this API process.

    The server-side candidate check prevents future calls in every process. A
    cross-process call may complete at its remote provider, so side effects are
    still governed by confirmation and the external provider's own semantics.
    """
    task_ids = list((_active_binding_tasks.get(binding_id) or {}).keys())
    if not task_ids:
        return 0
    from app.agents.mcp.manager import cancel_task

    outcomes = await asyncio.gather(*(cancel_task(task_id) for task_id in task_ids), return_exceptions=True)
    return sum(result is True for result in outcomes)


async def get_bound_capabilities(user_id: str, scene: str, user_role: str) -> list[ToolCapability]:
    """Return enabled, configured, user-owned MCP capabilities.

    A temporary circuit-open state is not an authorization decision. Keep the
    capability visible so the model can receive a structured unavailable
    result and explain it; hiding it made a missing candidate indistinguishable
    from a model deciding not to call the tool.
    """
    try:
        uid = uuid.UUID(str(user_id))
    except (TypeError, ValueError):
        return []
    async with async_session_factory() as session:
        result = await session.execute(
            select(UserMcpToolBinding).where(
                UserMcpToolBinding.user_id == uid,
                UserMcpToolBinding.status == "enabled",
            )
        )
        bindings = list(result.scalars())
    from app.agents.mcp.manager import server_is_healthy

    capabilities: list[ToolCapability] = []
    for binding in bindings:
        if scene not in set(binding.scenes or []) or not role_allows(binding.permission, user_role):
            continue
        if _configured_external_server(binding.server_name) is None:
            continue
        availability_hint = "available" if server_is_healthy(binding.server_name) else "circuit_breaker"
        schema = binding.input_schema if isinstance(binding.input_schema, dict) else {"type": "object", "properties": {}}
        capabilities.append(ToolCapability(
            name=f"mcp__{binding.server_name}__{binding.raw_tool_name}",
            version="1.0.0",
            status="stable",
            schema_fingerprint=_fingerprint(binding.server_name, binding.raw_tool_name, schema),
            description=binding.description,
            category="mcp",
            domain=binding.domain,
            intent_tags=list(binding.intent_tags or []),
            parameters=schema,
            source="mcp",
            server=binding.server_name,
            raw_name=binding.raw_tool_name,
            permission=binding.permission,
            write_op=binding.write_op,
            # 兼容首版之前已写入的绑定：外部 MCP 必须始终进入服务端精确参数
            # 指纹确认，避免旧记录成为绕过确认的持久化漏洞。
            requires_confirmation=True,
            confirmation_mode=binding.confirmation_mode,
            idempotent=bool(binding.idempotent and not binding.write_op),
            resource_templates=list(binding.resource_templates or []),
            annotations={
                "binding_id": str(binding.id),
                # 旧迁移之前创建的记录和轻量测试替身没有配额列时，使用平台
                # 默认值；升级后数据库列始终会有确定值。
                "daily_call_limit": int(getattr(binding, "daily_call_limit", settings.MCP_EXTERNAL_DEFAULT_DAILY_CALL_LIMIT)),
                "concurrency_limit": int(getattr(binding, "concurrency_limit", settings.MCP_EXTERNAL_DEFAULT_CONCURRENCY_LIMIT)),
                "availability_hint": availability_hint,
            },
        ))
    return capabilities


async def acquire_call_quota(binding_id: str, user_id: str, daily_limit: int, concurrency_limit: int) -> tuple[bool, str | None]:
    """Acquire an external MCP call slot in Redis, failing closed on uncertainty.

    A slot has a bounded TTL so worker crashes cannot permanently consume it.
    The daily counter is incremented only after capacity is available.
    """
    from datetime import datetime, timezone

    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    base = f"mcp:binding:{binding_id}:user:{user_id}"
    daily_key, active_key = f"{base}:daily:{day}", f"{base}:active"
    script = """
    local active = tonumber(redis.call('GET', KEYS[2]) or '0')
    if active >= tonumber(ARGV[2]) then return {0, 'CONCURRENCY_LIMIT'} end
    local used = tonumber(redis.call('GET', KEYS[1]) or '0')
    if used >= tonumber(ARGV[1]) then return {0, 'DAILY_LIMIT'} end
    redis.call('INCR', KEYS[1])
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
    redis.call('INCR', KEYS[2])
    redis.call('EXPIRE', KEYS[2], tonumber(ARGV[4]))
    return {1, ''}
    """
    try:
        result = await get_redis().eval(
            script, 2, daily_key, active_key, max(1, daily_limit), max(1, concurrency_limit), 172800, 600,
        )
        allowed, reason = int(result[0]), str(result[1] or "")
        return bool(allowed), (reason or None)
    except Exception:
        return False, "QUOTA_UNAVAILABLE"


async def release_call_quota(binding_id: str, user_id: str) -> None:
    base = f"mcp:binding:{binding_id}:user:{user_id}"
    try:
        await get_redis().eval(
            "local n=redis.call('DECR', KEYS[1]); if n<=0 then redis.call('DEL', KEYS[1]) end; return n",
            1, f"{base}:active",
        )
    except Exception:
        # Slot TTL prevents a leaked capacity record from becoming permanent.
        return
