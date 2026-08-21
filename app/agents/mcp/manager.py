"""MCP 客户端管理器：连接可插拔的 MCP 服务器（如 Electron 端暴露的本地工具）.

混合架构：
  - 服务端技能：原生 Python 函数（现有 plugins/skills，environment=server）；
  - 客户端技能：通过 MCP 调用（Electron 端跑 MCP server，可插拔，断开时回退 Redis 轮询）。

配置（config.MCP_SERVERS）：[{"name": "lumi_client", "transport": "streamable-http",
"url": "http://127.0.0.1:8765/mcp"}]

实现说明：
  mcp 2.0 的 streamable_http_client 是 async 上下文管理器，产出 (read_stream, write_stream)
  元组，且其 anyio 任务组必须在**同一任务内**进出；因此这里采用"每次调用一个短会话"，
  Electron 本地直连的握手开销可忽略（毫秒级），并彻底避免跨任务关闭的 RuntimeError。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from loguru import logger

from app.core.config import settings
from app.core.resilience import CircuitOpenError, get_breaker
from app.agents.skills.base import Skill, SkillContext, SkillResult

# 连接失败冷却：Electron 未启动/后端先于前端启动时，避免每次调用都重试并刷日志
_RETRY_COOLDOWN_S = 30.0
_failed_until: dict[str, float] = {}
_tools_cache: dict[str, tuple[float, list[dict]]] = {}
_session_workers: dict[str, "_McpSessionWorker"] = {}
_active_calls: dict[str, asyncio.Task] = {}
_active_requests: dict[str, tuple[object, int | str]] = {}

# Registered Skills use this gateway as their single execution boundary.  A
# client Skill is sent to the Electron MCP server when the server exposes the
# same tool; server/sandbox Skills are executed in-process behind the same
# result contract.  This keeps scheduling, timeout and audit callers agnostic
# to where a capability lives while retaining the Redis fallback for clients
# that have not upgraded their Electron runtime yet.
LOCAL_SKILL_SERVER = "lumi_skill"


class _McpSessionWorker:
    """Keep one MCP session in one asyncio task; calls are serialized per server."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.queue: asyncio.Queue[tuple[Callable[[object], Awaitable[object]], asyncio.Future]] = asyncio.Queue()
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(str(self.cfg["url"])) as streams:
            # MCP Python SDK 1.x 返回 (read_stream, write_stream)，较新的
            # streamable-http 实现会额外返回 session-id getter。只取前两个
            # 传输流，兼容两个版本，避免工具发现阶段因“too many values to
            # unpack”卡住，进而让 agent 误判所有 MCP 工具不可用。
            read_stream, write_stream = streams[0], streams[1]
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                while True:
                    fn, future = await self.queue.get()
                    if future.cancelled():
                        continue
                    try:
                        result = await fn(session)
                    except Exception as exc:  # noqa: BLE001
                        if not future.done():
                            future.set_exception(exc)
                    else:
                        if not future.done():
                            future.set_result(result)

    async def call(self, fn: Callable[[object], Awaitable[object]]) -> object:
        if self.task.done():
            await self.task
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        await self.queue.put((fn, future))
        return await future

    async def close(self) -> None:
        if not self.task.done():
            self.task.cancel()
        try:
            await self.task
        except (asyncio.CancelledError, Exception):
            pass


def _server_cfg(name: str) -> dict | None:
    for s in settings.MCP_SERVERS or []:
        if s.get("name") == name:
            return s
    return None


async def _call_with_session(
    name: str,
    fn: Callable[[object], Awaitable[object]],
) -> object | None:
    """连接 MCP 服务器并调用 fn(session)；失败返回 None（调用方降级）."""
    cfg = _server_cfg(name)
    if not cfg:
        return None
    if name in _failed_until and time.monotonic() < _failed_until[name]:
        return None
    try:
        async def _invoke() -> object:
            worker = _session_workers.get(name)
            if worker is None or worker.task.done():
                worker = _McpSessionWorker(cfg)
                _session_workers[name] = worker
            try:
                return await worker.call(fn)
            except Exception:
                if _session_workers.get(name) is worker:
                    _session_workers.pop(name, None)
                await worker.close()
                raise

        return await get_breaker(f"mcp:{name}:{cfg.get('url', '')}").call(_invoke)
    except CircuitOpenError as exc:
        logger.info("[MCP] 服务器 {} 暂时熔断，跳过调用: {}", name, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[MCP] 调用服务器 {} 失败（将回退轮询）: {}", name, exc)
        _failed_until[name] = time.monotonic() + _RETRY_COOLDOWN_S
        return None


async def list_tools(name: str) -> list[dict]:
    """列出 MCP 服务器暴露的工具."""

    cached = _tools_cache.get(name)
    ttl = max(0.0, float(getattr(settings, "MCP_TOOLS_CACHE_TTL_S", 30.0)))
    if cached and (ttl <= 0 or time.monotonic() - cached[0] < ttl):
        return [dict(item) for item in cached[1]]

    async def _list(session) -> list[dict]:
        res = await session.list_tools()
        result = []
        for t in (res.tools or []):
            annotations_obj = getattr(t, "annotations", None)
            annotations = (
                annotations_obj.model_dump(exclude_none=True)
                if hasattr(annotations_obj, "model_dump")
                else dict(annotations_obj or {})
            )
            meta_obj = getattr(t, "meta", None) or getattr(t, "_meta", None) or {}
            meta = (
                meta_obj.model_dump(exclude_none=True)
                if hasattr(meta_obj, "model_dump")
                else dict(meta_obj or {})
            )
            lumi = dict((meta or {}).get("lumi") or {})
            has_lumi = bool(lumi)
            read_only = bool(annotations.get("readOnlyHint", annotations.get("read_only_hint", False)))
            destructive = bool(
                annotations.get("destructiveHint", annotations.get("destructive_hint", False))
            )
            idempotent = bool(
                annotations.get("idempotentHint", annotations.get("idempotent_hint", False))
            )
            result.append({
                "name": t.name,
                "description": t.description,
                "input_schema": getattr(t, "inputSchema", None)
                or getattr(t, "input_schema", None)
                or {"type": "object", "properties": {}},
                "annotations": annotations,
                "permission": str(lumi.get("permission") or "user"),
                "write_op": bool(lumi.get("write_op", destructive or not read_only)),
                "requires_confirmation": bool(
                    lumi.get("requires_confirmation", destructive or (not read_only and not has_lumi))
                ),
                "confirmation_mode": str(
                    lumi.get("confirmation_mode") or ("client" if has_lumi else "server")
                ),
                "idempotent": bool(lumi.get("idempotent", idempotent or read_only)),
                "resource_templates": list(lumi.get("resource_templates") or []),
            })
        return result

    result = await _call_with_session(name, _list)
    tools = result if isinstance(result, list) else []
    if tools:
        _tools_cache[name] = (time.monotonic(), [dict(item) for item in tools])
    return tools


async def list_all_tools() -> list[dict]:
    """并发发现全部 MCP 工具，并生成不会与本地 Skill 冲突的限定名."""
    servers = [s for s in (settings.MCP_SERVERS or []) if s.get("name")]
    if not servers:
        return []
    discovered = await asyncio.gather(
        *(list_tools(str(server["name"])) for server in servers),
        return_exceptions=True,
    )
    result: list[dict] = []
    for server, tools in zip(servers, discovered, strict=False):
        if isinstance(tools, Exception):
            continue
        server_name = str(server["name"])
        for tool in tools:
            raw_name = str(tool.get("name") or "")
            if not raw_name:
                continue
            result.append(
                {
                    **tool,
                    "server": server_name,
                    "raw_name": raw_name,
                    "name": f"mcp__{server_name}__{raw_name}",
                }
            )
    return result


async def call_tool(
    name: str,
    tool_name: str,
    args: dict | None = None,
    *,
    task_id: str | None = None,
    timeout_s: float | None = None,
    on_progress: Callable[[dict], Any] | None = None,
) -> dict | None:
    """调用 MCP 工具。

    ``task_id`` 作为标准 MCP ``_meta`` 扩展传递，进度使用 SDK 的
    ``progress_callback``。业务层仍可通过返回的 metadata 关联审计记录。
    """

    async def _call(session) -> dict:
        call_kwargs: dict[str, Any] = {}
        effective_timeout = timeout_s if timeout_s is not None else float(
            getattr(settings, "MCP_TOOL_TIMEOUT_S", 180.0)
        )
        if effective_timeout > 0:
            call_kwargs["read_timeout_seconds"] = timedelta(seconds=effective_timeout)
        if on_progress:
            async def _progress(progress: float, total: float | None = None, message: str | None = None):
                event = {
                    "type": "mcp_progress", "task_id": task_id, "progress": progress,
                    "total": total, "message": message or "",
                }
                value = on_progress(event)
                if hasattr(value, "__await__"):
                    await value
            call_kwargs["progress_callback"] = _progress
        if task_id:
            # MCP 标准字段用于请求关联；``lumi.task_id`` 仅供当前 Electron
            # 服务端将进度/审计映射回本应用任务。
            call_kwargs["meta"] = {
                "progressToken": task_id,
                "io.modelcontextprotocol/related-task": {"taskId": task_id},
                "lumi": {"task_id": task_id},
            }
            # Python MCP 1.x 尚未公开暴露 call_tool 的 JSON-RPC request id。
            # 同一 server worker 内调用串行，故在发起请求前读取 SDK 的递增 id
            # 可安全用于发送标准 notifications/cancelled；若 SDK 将来提供公开
            # request handle，可在此替换，不改变上层 cancel_task 接口。
            request_id = getattr(session, "_request_id", None)
            if isinstance(request_id, (int, str)):
                _active_requests[task_id] = (session, request_id)
        try:
            res = await session.call_tool(tool_name, args or {}, **call_kwargs)
        except TypeError:
            # 兼容旧版/测试客户端不接受新增 MCP 参数时的安全降级。
            res = await session.call_tool(tool_name, args or {})
        finally:
            if task_id:
                _active_requests.pop(task_id, None)
        content = getattr(res, "content", None) or []
        text = "".join(
            str(c.text)
            for c in content
            if hasattr(c, "text") and getattr(c, "text", None)
        )
        structured = getattr(res, "structured_content", None) or getattr(
            res, "structuredContent", None
        )
        return {
            "success": True,
            "content": text,
            "metadata": structured or {},
            "is_error": bool(
                getattr(res, "is_error", None) is True
                or getattr(res, "isError", False)
            ),
            "task_id": task_id,
        }

    current = asyncio.current_task()
    if task_id and current:
        _active_calls[task_id] = current
    try:
        operation = _call_with_session(name, _call)
        effective_timeout = timeout_s if timeout_s is not None else float(
            getattr(settings, "MCP_TOOL_TIMEOUT_S", 180.0)
        )
        if effective_timeout > 0:
            return await asyncio.wait_for(operation, timeout=effective_timeout + 5)
        return await operation
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        if task_id:
            await _notify_remote_cancel(task_id, "MCP tool deadline exceeded")
        return {
            "success": False, "content": "MCP 工具执行超时", "metadata": {"task_id": task_id},
            "is_error": True, "error_code": "MCP_TIMEOUT", "task_id": task_id,
        }
    finally:
        if task_id and _active_calls.get(task_id) is current:
            _active_calls.pop(task_id, None)


async def call_skill(
    skill: Skill,
    args: dict | None = None,
    *,
    context: SkillContext | None = None,
    task_id: str | None = None,
    timeout_s: float | None = None,
    on_progress: Callable[[dict], Any] | None = None,
    execution_policy: dict | None = None,
) -> dict:
    """Execute any registered Skill through the unified MCP gateway.

    Client skills prefer the real Electron MCP endpoint when it advertises the
    skill name.  If the desktop is not connected, the legacy per-user request
    queue remains a safe compatibility fallback.  Backend and sandbox skills
    run locally because their resources (DB, uploads and Docker sandbox) live
    inside the API worker; they still return the same MCP-shaped envelope to
    the executor.
    """
    # Tool arguments are model-controlled.  A policy is accepted only from the
    # executor after it has inspected the current user message, then injected
    # immediately before the trusted Electron MCP hop.
    args = dict(args or {})
    args.pop("_lumi_execution_policy", None)
    if execution_policy:
        args["_lumi_execution_policy"] = {
            "explicit_user_delete": bool(execution_policy.get("explicit_user_delete")),
        }
    if skill.environment == "client":
        for cfg in settings.MCP_SERVERS or []:
            server_name = str(cfg.get("name") or "")
            if not server_name:
                continue
            advertised = await list_tools(server_name)
            if any(str(item.get("name")) == skill.name for item in advertised):
                raw = await call_tool(
                    server_name,
                    skill.name,
                    args,
                    task_id=task_id,
                    timeout_s=timeout_s,
                    on_progress=on_progress,
                )
                if raw is not None:
                    # Keep the transport visible to the scheduler/audit layer.
                    # An MCP tool error is still an MCP execution result and
                    # must not be silently retried through the legacy queue.
                    is_error = bool(raw.get("is_error")) or not bool(raw.get("success", True))
                    return {
                        **raw,
                        "error_code": raw.get("error_code") or (
                            "MCP_EXEC_ERROR" if is_error else None
                        ),
                        "retryable": bool(raw.get("retryable", False)),
                        "metadata": {
                            "skill": skill.name,
                            "transport": "mcp",
                            "server": server_name,
                            **(raw.get("metadata") or {}),
                        },
                    }
                break

    try:
        effective_timeout = timeout_s if timeout_s is not None else float(
            getattr(settings, "MCP_TOOL_TIMEOUT_S", 180.0)
        )
        operation = skill.execute(args, context)
        result: SkillResult = await asyncio.wait_for(operation, effective_timeout) if effective_timeout > 0 else await operation
    except asyncio.TimeoutError:
        return {
            "success": False,
            "content": "技能执行超时",
            "metadata": {
                "skill": skill.name,
                "task_id": task_id,
                "transport": "in_process_adapter",
                "server": LOCAL_SKILL_SERVER,
            },
            "is_error": True,
            "error_code": "MCP_TIMEOUT",
            "retryable": True,
            "task_id": task_id,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "content": str(exc) or "技能执行失败",
            "metadata": {
                "skill": skill.name,
                "task_id": task_id,
                "transport": "in_process_adapter",
                "server": LOCAL_SKILL_SERVER,
            },
            "is_error": True,
            "error_code": "MCP_EXEC_ERROR",
            "retryable": True,
            "task_id": task_id,
        }
    if not isinstance(result, SkillResult):
        result = SkillResult(success=False, error="技能返回结果无效", error_code="EXEC_ERROR")
    return {
        "success": bool(result.success),
        "content": result.output if result.success else (result.error or "技能执行失败"),
        "metadata": {
            "skill": skill.name,
            "task_id": task_id,
            "transport": "in_process_adapter",
            "server": LOCAL_SKILL_SERVER,
            **(result.metadata or {}),
        },
        "is_error": not bool(result.success),
        "error_code": result.error_code,
        "task_id": task_id,
    }


async def _notify_remote_cancel(task_id: str, reason: str) -> None:
    """Best-effort standard MCP cancellation notification for one in-flight call."""
    active_request = _active_requests.get(str(task_id))
    if not active_request:
        return
    session, request_id = active_request
    try:
        from mcp.types import CancelledNotification, CancelledNotificationParams

        await session.send_notification(CancelledNotification(
            params=CancelledNotificationParams(requestId=request_id, reason=reason)
        ))
    except Exception as exc:  # noqa: BLE001
        logger.debug("发送 MCP 取消通知失败，继续本地取消: {}", exc)


async def cancel_task(task_id: str) -> bool:
    """取消正在执行的 MCP 调用，并向兼容服务器发送标准取消通知。"""
    key = str(task_id)
    task = _active_calls.get(key)
    if not task or task.done():
        return False
    await _notify_remote_cancel(key, "Cancelled by Lumi user")
    task.cancel()
    return True


async def close_all() -> None:
    """清理客户端状态，便于应用退出或 MCP 配置刷新后重新发现。"""
    _failed_until.clear()
    _tools_cache.clear()
    _active_calls.clear()
    _active_requests.clear()
    workers = list(_session_workers.values())
    _session_workers.clear()
    await asyncio.gather(*(worker.close() for worker in workers), return_exceptions=True)
