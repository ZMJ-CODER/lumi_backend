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

from loguru import logger

from app.core.config import settings

# 连接失败冷却：Electron 未启动/后端先于前端启动时，避免每次调用都重试并刷日志
_RETRY_COOLDOWN_S = 30.0
_failed_until: dict[str, float] = {}


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
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        # 每次调用使用独立短会话。不同 DAG 步骤之间不共享 session，也不使用
        # 全局服务器锁，因此没有依赖关系的步骤可以真正并行执行。
        async with streamable_http_client(str(cfg["url"])) as streams:
            read_stream, write_stream = streams
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                return await fn(session)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[MCP] 调用服务器 {} 失败（将回退轮询）: {}", name, exc)
        _failed_until[name] = time.monotonic() + _RETRY_COOLDOWN_S
        return None


async def list_tools(name: str) -> list[dict]:
    """列出 MCP 服务器暴露的工具."""

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
    return result if isinstance(result, list) else []


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


async def call_tool(name: str, tool_name: str, args: dict | None = None) -> dict | None:
    """调用 MCP 工具；失败返回 None（调用方回退）."""

    async def _call(session) -> dict:
        res = await session.call_tool(tool_name, args or {})
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
        }

    return await _call_with_session(name, _call)


async def close_all() -> None:
    """清理（短会话无持久连接；重置失败冷却，便于下次重试）."""
    _failed_until.clear()
