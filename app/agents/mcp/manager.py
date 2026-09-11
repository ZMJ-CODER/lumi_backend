"""MCP 客户端管理器：连接可插拔的 MCP 服务器（如 Electron 端暴露的本地工具）.

混合架构：
  - 服务端工具：原生 Python 函数（plugins/tools，environment=server）；
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
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from loguru import logger

from app.core.config import settings
from app.core.resilience import CircuitOpenError, get_breaker
from app.agents.orchestration.timeout_ladder import DeadlineExceeded
from app.agents.skills.base import SkillContext, SkillProgress, Tool
from app.agents.skills.output_contract import OutputMeta, ToolOutput
from app.services.tool_output_pipeline import normalize_execution_envelope, to_execution_envelope

# 连接失败冷却：Electron 未启动/后端先于前端启动时，避免每次调用都重试并刷日志
_RETRY_COOLDOWN_S = 30.0
_failed_until: dict[str, float] = {}
_tools_cache: dict[str, tuple[float, list[dict]]] = {}
_session_workers: dict[str, "_McpSessionWorker"] = {}
_active_calls: dict[str, asyncio.Task] = {}
_active_requests: dict[str, tuple[object, int | str]] = {}

# Registered Skills use this gateway as their single execution boundary.  A
# client Tool is sent to the Electron MCP server when the server exposes the
# same tool; server/sandbox Tool is executed in-process behind the same
# result contract.  This keeps scheduling, timeout and audit callers agnostic
# to where a capability lives while retaining the Redis fallback for clients
# that have not upgraded their Electron runtime yet.
LOCAL_SKILL_SERVER = "lumi_skill"


def _loopback_health_url(cfg: dict) -> str:
    """Return Lumi desktop's health endpoint for a loopback MCP URL only."""

    try:
        parsed = urlsplit(str(cfg.get("url") or ""))
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "127.0.0.1", "localhost", "::1",
    }:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, "/health", "", ""))


def _call_timeout_seconds() -> float:
    """MCP 单次会话调用的上界（桌面能力发现/健康探测必须快速失败并降级）。

    取 ``AGENT_MCP_DISCOVERY_TIMEOUT_SECONDS``（默认 5s，可调），并夹在超时阶梯的
    合法区间内；任何情况下都不返回 0/None，避免退化成无界等待。
    """
    from app.agents.orchestration.timeout_ladder import (
        MAX_OVERRIDE_SECONDS,
        MIN_OVERRIDE_SECONDS,
    )

    try:
        configured = float(getattr(settings, "AGENT_MCP_DISCOVERY_TIMEOUT_SECONDS", 5.0))
    except (TypeError, ValueError):
        configured = 5.0
    if configured <= 0:
        configured = 5.0
    return min(max(configured, MIN_OVERRIDE_SECONDS), MAX_OVERRIDE_SECONDS)


async def _loopback_server_has_recovered(cfg: dict) -> bool:
    """Probe a restarted local Electron service without weakening remote cooldowns."""

    health_url = _loopback_health_url(cfg)
    if not health_url:
        return False
    try:
        import httpx

        async with httpx.AsyncClient(timeout=1.5, trust_env=False) as client:
            response = await client.get(health_url)
        if response.status_code != 200:
            return False
        payload = response.json()
        return isinstance(payload, dict) and payload.get("success") is not False
    except Exception:  # noqa: BLE001 - a health probe must never break routing
        return False


def _desktop_workspace_alias(skill_name: str, args: dict, *, task_id: str | None) -> tuple[str, dict] | None:
    """Translate legacy server-side Tool names to Electron's atomic API.

    This is a transport compatibility adapter, not a model routing rule. The
    model keeps its governed vocabulary; Electron receives only formal
    workspace/sandbox operations.
    """
    project_id = str(args.get("workspace_id") or args.get("project_id") or "").strip()
    if not project_id:
        return None
    path = str(args.get("path") or args.get("file_path") or "").strip()
    from app.services.workspace_context import WORKSPACE_NAVIGATOR

    if skill_name == WORKSPACE_NAVIGATOR:
        # 聚合读取入口的传输兼容别名：老客户端只实现了原子 workspace_read 时，
        # read 动作仍可投递（list/search 需要客户端升级）。模型可见的正式工具名
        # 始终是聚合入口，别名只在传输层使用。
        action = str(args.get("action") or "").strip().casefold()
        if action != "read" or not path:
            return None
        return "workspace_read", {
            "workspace_id": project_id,
            "path": path,
            "max_chars": args.get("max_chars") or 200000,
        }
    if skill_name == "Read" and path:
        return "workspace_read", {"workspace_id": project_id, "path": path, "max_chars": args.get("limit", 200000)}
    if skill_name == "Write" and path:
        return "workspace_stage_write", {"workspace_id": project_id, "path": path, "content": str(args.get("content") or "")}
    if skill_name == "Glob":
        return "workspace_list", {"workspace_id": project_id, "path": str(args.get("path") or ""), "include_hidden": bool(args.get("include_hidden"))}
    if skill_name == "Grep":
        return "workspace_search", {"workspace_id": project_id, "query": str(args.get("pattern") or args.get("query") or ""), "path": str(args.get("path") or ""), "max_results": args.get("max_results", 30)}
    if skill_name == "run_in_sandbox":
        return "sandbox_run", {"workspace_id": project_id, "command": str(args.get("command") or ""), "cwd": str(args.get("cwd") or ""), "timeout": args.get("timeout", 60)}
    if skill_name == "sandbox_reset":
        return "sandbox_reset", {"workspace_id": project_id}
    if skill_name == "sandbox_commit":
        return "workspace_commit", {
            "workspace_id": project_id,
            "base_version": args.get("base_version"),
            "idempotency_key": str(args.get("idempotency_key") or f"{task_id or 'desktop'}:workspace_commit:{project_id}"),
            "approved": bool(args.get("approved")),
        }
    return None


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
        # 有界等待：worker 任务若卡在握手/会话初始化（桌面端未启动、socket 半开、
        # 版本不兼容），这个 future 永远不会被 set，调用方此前会**无界**挂住——
        # 计划编译期发现桌面能力正是这样把一次 submit_job 卡死的。超时后取消
        # future 并让调用方走既有降级（工具集为空/熔断冷却）。
        from app.agents.orchestration.timeout_ladder import enforce

        return await enforce(
            future,
            seconds=_call_timeout_seconds(),
            label="MCP 会话调用",
        )

    async def close(self) -> None:
        if not self.task.done():
            self.task.cancel()
        try:
            await self.task
        except (asyncio.CancelledError, Exception):
            pass


def _server_cfg(name: str) -> dict | None:
    from app.agents.mcp.desktop_connections import desktop_connections

    return desktop_connections.config_for(name)


def server_is_healthy(name: str) -> bool:
    """Whether a configured server is outside its local failure cooldown.

    This intentionally exposes no transport internals. Candidate routing uses
    it to hide a temporarily broken external capability before an LLM wastes a
    tool round; the call path still remains the final source of truth.
    """
    if _server_cfg(name) is None:
        return False
    until = _failed_until.get(name)
    return until is None or time.monotonic() >= until


async def ensure_server_healthy(name: str) -> bool:
    """Refresh loopback health during cooldown before routing hides a desktop."""

    cfg = _server_cfg(name)
    if cfg is None:
        return False
    until = _failed_until.get(name)
    if until is None or time.monotonic() >= until:
        return True
    # 探测本身也必须有界：健康探测卡住同样会把提交路径钉死在这里。
    from app.agents.orchestration.timeout_ladder import DeadlineExceeded, enforce

    try:
        recovered = await enforce(
            _loopback_server_has_recovered(cfg),
            seconds=_call_timeout_seconds(),
            label="MCP 健康探测",
        )
    except DeadlineExceeded:
        return False
    if not recovered:
        return False
    stale_worker = _session_workers.pop(name, None)
    if stale_worker is not None:
        await stale_worker.close()
    _failed_until.pop(name, None)
    invalidate_tool_cache(name)
    # The same transport failure is also tracked by the generic breaker.  A
    # successful local health probe is an explicit half-open success signal;
    # clear that state so the immediately following MCP handshake may run.
    await get_breaker(f"mcp:{name}:{cfg.get('url', '')}").record_success()
    return True


def invalidate_tool_cache(name: str | None = None) -> None:
    """Forget discovery data after a user binding changes or a server reconnects."""
    if name:
        _tools_cache.pop(name, None)
    else:
        _tools_cache.clear()


async def _call_with_session(
    name: str,
    fn: Callable[[object], Awaitable[object]],
) -> object | None:
    """连接 MCP 服务器并调用 fn(session)；失败返回 None（调用方降级）."""
    cfg = _server_cfg(name)
    if not cfg:
        return None
    if name in _failed_until and time.monotonic() < _failed_until[name]:
        # The desktop commonly restarts while the API process stays alive.
        # A fixed cooldown made the newly healthy client look unavailable for
        # another 30 seconds and let stale discovery data survive the restart.
        # Only loopback Lumi endpoints expose this recovery probe; arbitrary
        # remote MCP servers retain the normal circuit/cooldown behaviour.
        if not await ensure_server_healthy(name):
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

        result = await get_breaker(f"mcp:{name}:{cfg.get('url', '')}").call(_invoke)
        _failed_until.pop(name, None)
        return result
    except CircuitOpenError as exc:
        logger.info("[MCP] 服务器 {} 暂时熔断，跳过调用: {}", name, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        # 超时（``DeadlineExceeded``）与普通失败走同一降级路径：失效工具缓存并进入
        # 冷却，下一次调用由健康探测决定是否重建会话（会话 worker 已在上面关闭）。
        if isinstance(exc, DeadlineExceeded):
            logger.warning(
                "[MCP] 服务器 {} 会话调用超时（{}s），已降级", name, getattr(exc, "timeout", "")
            )
        else:
            logger.warning("[MCP] 调用服务器 {} 失败（将回退轮询）: {}", name, exc)
        invalidate_tool_cache(name)
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
            mapped = {
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
            }
            # Keep the legacy discovery shape for ordinary MCP servers while
            # preserving plugin metadata when a desktop plugin declares it.
            for key in ("version", "domain", "plugin_id", "plugin_version"):
                value = lumi.get(key)
                if value not in (None, ""):
                    mapped[key] = str(value)
            result.append(mapped)
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
    call_id: str | None = None,
    timeout_s: float | None = None,
    on_progress: Callable[[dict], Any] | None = None,
    user_id: str = "",
    device_id: str = "",
    workspace_id: str = "",
    conversation_id: str = "",
    route: dict | None = None,
) -> dict | None:
    """调用 MCP 工具。

    ``task_id`` 作为标准 MCP ``_meta`` 扩展传递，进度使用 SDK 的
    ``progress_callback``。业务层仍可通过返回的 metadata 关联审计记录。

    ``user_id / device_id / workspace_id / conversation_id`` 只用于把请求
    身份透传给 Electron（路由到托管该工作区的设备、供其审计/归属校验），
    服务端的授权判定始终发生在调用本函数之前。

    ``route``（可选）表达"这次调用**由哪个 Provider/租约**承接"：能力派发适配层会带上
    ``{"provider_id": ..., "plugin_id": ..., "lease_id": ...}``。它随 ``_meta.lumi``
    透传给 Electron（供其核对是否与自己的租约一致），**不改变**路由选择本身；
    缺省为空 = 旧行为（按工具名调用默认本地实现）。
    """

    call_id = str(call_id or uuid.uuid4())
    route_meta = {
        key: str(value)
        for key, value in dict(route or {}).items()
        if key in {"provider_id", "plugin_id", "lease_id", "capability"} and value not in (None, "")
    }
    if route_meta:
        # 派发关联必须一路带到客户端：它用 provider_id/lease_id 核对"服务端确实按租约派的"。
        args = {**(args or {}), "_lumi_route": route_meta}

    async def _call(session) -> dict:
        call_kwargs: dict[str, Any] = {}
        effective_timeout = timeout_s if timeout_s is not None else float(
            getattr(settings, "MCP_TOOL_TIMEOUT_S", 180.0)
        )
        if effective_timeout > 0:
            call_kwargs["read_timeout_seconds"] = timedelta(seconds=effective_timeout)
        if on_progress:
            async def _progress(progress: float, total: float | None = None, message: str | None = None):
                percentage = (progress / total * 100) if total and total > 0 else progress
                event = {
                    "type": "mcp_progress",
                    **SkillProgress(
                        task_id=task_id or "", job_id=task_id or "", skill_name=tool_name,
                        phase="executing", percentage=percentage, message=message or "",
                    ).model_dump(),
                    "total": total,
                }
                value = on_progress(event)
                if hasattr(value, "__await__"):
                    await value
            call_kwargs["progress_callback"] = _progress
        if task_id:
            # MCP 标准字段用于请求关联；``lumi.task_id`` 仅供当前 Electron
            # 服务端将进度/审计映射回本应用任务。
            lumi_meta: dict[str, object] = {
                "task_id": task_id,
                "call_id": call_id,
            }
            for key, value in (
                ("user_id", user_id),
                ("device_id", device_id),
                ("workspace_id", workspace_id),
                ("conversation_id", conversation_id or task_id),
            ):
                if value not in (None, ""):
                    lumi_meta[key] = str(value)
            if route_meta:
                # 路由/租约信息与身份并列，客户端据此核对归属。
                lumi_meta["route"] = route_meta
            call_kwargs["meta"] = {
                "progressToken": task_id,
                "io.modelcontextprotocol/related-task": {"taskId": task_id},
                "lumi": lumi_meta,
            }
            # Python MCP 1.x 尚未公开暴露 call_tool 的 JSON-RPC request id。
            # 同一 server worker 内调用串行，故在发起请求前读取 SDK 的递增 id
            # 可安全用于发送标准 notifications/cancelled；若 SDK 将来提供公开
            # request handle，可在此替换，不改变上层 cancel_task 接口。
            request_id = getattr(session, "_request_id", None)
            if isinstance(request_id, (int, str)):
                _active_requests[task_id] = (session, request_id)
        try:
            tool_args = {**(args or {}), "_lumi_call_id": call_id}
            res = await session.call_tool(tool_name, tool_args, **call_kwargs)
        except TypeError:
            # 兼容旧版/测试客户端不接受新增 MCP 参数时的安全降级。
            res = await session.call_tool(tool_name, {**(args or {}), "_lumi_call_id": call_id})
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
        is_error = bool(
            getattr(res, "is_error", None) is True
            or getattr(res, "isError", False)
        )
        # Electron returns the canonical execution envelope in
        # ``structuredContent``.  Unwrap it here: feeding the entire envelope
        # back as ``data`` creates a data-within-data nesting on every MCP
        # hop, which obscures workspace versions from the orchestration layer.
        is_envelope = isinstance(structured, dict) and "status" in structured and "data" in structured
        data = structured.get("data") if is_envelope else (structured if structured is not None else text)
        # Preserve user-readable text from a structured desktop result.  The
        # structured payload also carries workspace metadata, so moving text
        # into ``data`` would force every downstream Skill to know transport
        # details.  Canonical result data stays structured; the model-facing
        # projection can prefer its ``content`` key.
        structured_status = str(structured.get("status") or "") if isinstance(structured, dict) else ""
        raw_meta = (structured or {}).get("meta") if isinstance(structured, dict) else None
        try:
            output_meta = OutputMeta.model_validate(raw_meta or {})
        except (TypeError, ValueError):
            output_meta = OutputMeta()
        if isinstance(data, dict) and not output_meta.summary:
            output_meta = output_meta.model_copy(update={"summary": str(data.get("content") or text[:500])})
        output_meta = output_meta.model_copy(update={
            "total_size": output_meta.total_size or len(text),
            "summary": output_meta.summary or (text[:500] if structured is not None else ""),
            "quality_hints": {
                **output_meta.quality_hints,
                **({"task_id": task_id} if task_id else {}),
            },
        })
        output = ToolOutput(
            call_id=str((structured or {}).get("call_id") or call_id) if isinstance(structured, dict) else call_id,
            status="failed" if is_error else (structured_status if structured_status in {"success", "partial", "empty", "pending", "pending_approval", "uncertain", "cancelled"} else ("empty" if not data else "success")),
            data=data,
            content_type=(str((structured or {}).get("content_type") or "structured") if isinstance(structured, dict) else "text"),
            meta=output_meta,
            error=(structured or {}).get("error") if isinstance(structured, dict) else (text or "MCP 工具执行失败" if is_error else None),
            error_code=(structured or {}).get("error_code") if isinstance(structured, dict) else ("MCP_EXEC_ERROR" if is_error else None),
        )
        return to_execution_envelope(output)

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
        return to_execution_envelope(ToolOutput(
            status="failed", data="MCP 工具执行超时", error="MCP 工具执行超时",
            error_code="MCP_TIMEOUT", retryable=True,
            meta=OutputMeta(quality_hints={"task_id": task_id} if task_id else {}),
        ))
    finally:
        if task_id and _active_calls.get(task_id) is current:
            _active_calls.pop(task_id, None)


async def call_skill(
    skill: Tool,
    args: dict | None = None,
    *,
    context: SkillContext | None = None,
    task_id: str | None = None,
    timeout_s: float | None = None,
    on_progress: Callable[[dict], Any] | None = None,
    execution_policy: dict | None = None,
    call_id: str | None = None,
) -> dict:
    """通过统一 MCP 网关执行一个已注册的原子 Tool。

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
        # Request identity for the Electron hop: the active workspace's
        # registered device is authoritative when present (see
        # workspace_context.resolve_workspace_desktop), otherwise the JWT user
        # and conversation are carried as-is for desktop-side attribution.
        call_user_id = str((context.user_id if context is not None else "") or "")
        call_conversation_id = str((context.conversation_id if context is not None else "") or task_id or "")
        call_workspace_id = str((context.workspace_id if context is not None else "") or "")
        call_device_id = ""
        if call_workspace_id and call_user_id:
            try:
                from app.services.workspace_context import resolve_workspace_desktop

                route = resolve_workspace_desktop(call_user_id, call_workspace_id)
                call_device_id = str(route.get("device_id") or "")
            except Exception:  # noqa: BLE001 - 身份解析失败不阻断调用
                call_device_id = ""
        for cfg in settings.MCP_SERVERS or []:
            server_name = str(cfg.get("name") or "")
            if not server_name:
                continue
            advertised = await list_tools(server_name)
            alias = _desktop_workspace_alias(skill.name, args, task_id=task_id)
            target_name, target_args = alias if alias else (skill.name, args)
            if any(str(item.get("name")) == target_name for item in advertised):
                # Historical sandbox_commit had no version input. Obtain the
                # current version immediately before requesting approval so
                # the eventual commit remains compare-and-swap protected.
                if target_name == "workspace_commit" and target_args.get("base_version") is None:
                    diff = await call_tool(
                        server_name, "workspace_diff", {"workspace_id": target_args["workspace_id"]},
                        task_id=task_id, timeout_s=timeout_s,
                        user_id=call_user_id, device_id=call_device_id,
                        workspace_id=call_workspace_id, conversation_id=call_conversation_id,
                    )
                    if diff is None or diff.get("status") == "failed":
                        return diff or to_execution_envelope(ToolOutput(
                            call_id=call_id, status="failed", data="无法读取工作区版本",
                            error="无法读取工作区版本", error_code="WORKSPACE_DIFF_FAILED",
                        ))
                    diff_output = normalize_execution_envelope(diff, tool_name="workspace_diff")
                    diff_data = diff_output.data if isinstance(diff_output.data, dict) else {}
                    diff_meta = diff_output.metadata.get("meta") if isinstance(diff_output.metadata, dict) else {}
                    target_args["base_version"] = diff_data.get(
                        "base_version", (diff_meta or {}).get("workspace_version")
                    )
                raw = await call_tool(
                    server_name,
                    target_name,
                    target_args,
                    task_id=task_id,
                    call_id=call_id,
                    timeout_s=timeout_s,
                    on_progress=on_progress,
                    user_id=call_user_id,
                    device_id=call_device_id,
                    workspace_id=call_workspace_id,
                    conversation_id=call_conversation_id,
                )
                if raw is not None:
                    # Keep the transport visible to the scheduler/audit layer.
                    # An MCP tool error is still an MCP execution result and
                    # must not be silently retried through the legacy queue.
                    # 信封解析只在契约适配器内部进行，这里不再手工读裸字典。
                    normalized = normalize_execution_envelope(raw, tool_name=target_name)
                    if normalized.status == "failed" and not normalized.error_code:
                        normalized = normalized.model_copy(update={"error_code": "MCP_EXEC_ERROR"})
                    return to_execution_envelope(
                        normalized,
                        transport_meta={
                            "skill": skill.name,
                            "tool": target_name,
                            "kind": "mcp",
                            "server": server_name,
                        },
                    )
                break

    try:
        effective_timeout = timeout_s if timeout_s is not None else float(
            getattr(settings, "MCP_TOOL_TIMEOUT_S", 180.0)
        )
        operation = skill.execute(args, context)
        result: ToolOutput = await asyncio.wait_for(operation, effective_timeout) if effective_timeout > 0 else await operation
    except asyncio.TimeoutError:
        return to_execution_envelope(
            ToolOutput(
                call_id=call_id,
                status="failed", data="技能执行超时", error="技能执行超时",
                error_code="MCP_TIMEOUT", retryable=True,
            ),
            transport_meta={
                "skill": skill.name, "kind": "in_process_adapter",
                "server": LOCAL_SKILL_SERVER, "task_id": task_id,
            },
        )
    except Exception as exc:  # noqa: BLE001
        error_code = "MCP_EXEC_ERROR"
        error_message = str(exc) or "技能执行失败"
        # Tool implementations may call the request-scoped model directly.
        # Preserve billing/auth/provider semantics so the DAG cannot retry or
        # replan them as if they were an ordinary tool error.
        lowered = error_message.lower()
        model_markers = (
            "api key", "unauthorized", "authentication", "insufficient balance",
            "quota", "payment required", "provider unavailable", "connection refused",
            "connection reset", "bad gateway", "service unavailable", "model not found",
            "余额", "欠费", "供应商",
        )
        if context is not None and context.scene == "office" and any(marker in lowered or marker in error_message for marker in model_markers):
            from app.agents.skills.recovery import classify_model_error

            error_code, error_message = classify_model_error(exc)
        return to_execution_envelope(
            ToolOutput(
                call_id=call_id,
                status="failed", data=error_message, error=error_message,
                error_code=error_code,
                retryable=False if error_code.startswith("MODEL_") else True,
            ),
            transport_meta={
                "skill": skill.name, "kind": "in_process_adapter",
                "server": LOCAL_SKILL_SERVER, "task_id": task_id,
            },
        )
    if not isinstance(result, ToolOutput):
        result = ToolOutput(status="failed", error="技能返回结果无效", error_code="EXEC_ERROR")
    return to_execution_envelope(
        result.model_copy(update={"call_id": result.call_id or call_id}),
        transport_meta={
            "skill": skill.name, "kind": "in_process_adapter",
            "server": LOCAL_SKILL_SERVER, "task_id": task_id,
        },
    )


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
