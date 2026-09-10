"""工作区上下文（Workspace Context）：Electron 本地工作区的只读摘要与状态。

架构约定（与前端方案一致）：
  - 前端负责维护并暴露本地工作区；后端只保存元数据与状态摘要，
    不保存任何工作区文件内容。
  - conversation_id -> workspace_id 绑定在 ``app.services.workspaces``；
    托管该工作区的桌面设备（device_id / device_server）也记录在那里。
  - 后端通过桌面 MCP 工具 ``workspace_catalog`` 获取目录摘要，回退到
    ``workspace_diff``（版本/暂存）+ ``workspace_list``（目录条目）。
  - 目录扫描结果按 ``{workspace_id}:{version}`` 缓存，version 取自
    ``workspace_diff`` 的 ``base_version``，文件变化后自动失配重建。

本模块不依赖任何 Agent/Planner/Skill 类型，纯服务层，可被入口编排、
Planner 摘要注入与工具执行器复用。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.services import workspaces

# ── 统一工作区状态码（模型与入口层可见的稳定枚举）──────────────
WORKSPACE_NOT_BOUND = "WORKSPACE_NOT_BOUND"
WORKSPACE_NOT_REGISTERED = "WORKSPACE_NOT_REGISTERED"
WORKSPACE_DEVICE_OFFLINE = "WORKSPACE_DEVICE_OFFLINE"
WORKSPACE_ROOT_MISSING = "WORKSPACE_ROOT_MISSING"
WORKSPACE_READ_FAILED = "WORKSPACE_READ_FAILED"
WORKSPACE_READY = "WORKSPACE_READY"

# 工作区“通用读取域”：任何绑定工作区的办公 Agent 都可以发现。
# ── 工作区完整能力集（按审批/风险语义分档）──────────────
# 这些常量是“能力目录”的权威分段：读取域对任何绑定工作区的 Agent 可见；
# 暂存写入/沙箱/提交能力仅当任务处于相应阶段且通过授权策略时才会注入工具
# 窗口，模型可见不等于可直接调用（调用仍受 workspace scope 与策略门约束）。
WORKSPACE_CONTENT_EXTRACT = "workspace_content_extract"

WORKSPACE_READ_CAPABILITIES = frozenset({
    "workspace_catalog",
    "workspace_list",
    "workspace_stat",
    "workspace_read",
    "workspace_search",
    # 统一内容提取：由 Electron 按文件类型在内部选择 docx/pptx/pdf/csv/图片
    # 等解析器；模型只理解“提取文件内容”，不区分格式工具。
    WORKSPACE_CONTENT_EXTRACT,
})
WORKSPACE_STAGE_WRITE_CAPABILITIES = frozenset({
    "workspace_stage_write",
    "workspace_stage_delete",
})
WORKSPACE_SANDBOX_CAPABILITIES = frozenset({
    "sandbox_prepare",
    "sandbox_run",
    "sandbox_output_read",
    "sandbox_reset",
})
WORKSPACE_COMMIT_CAPABILITIES = frozenset({
    "workspace_diff",
    "workspace_commit",
    "workspace_rollback",
})
WORKSPACE_HIGH_RISK_CAPABILITIES = frozenset({
    "workspace_rollback",
})
WORKSPACE_ALL_CAPABILITIES = frozenset({
    *WORKSPACE_READ_CAPABILITIES,
    *WORKSPACE_STAGE_WRITE_CAPABILITIES,
    *WORKSPACE_SANDBOX_CAPABILITIES,
    *WORKSPACE_COMMIT_CAPABILITIES,
})

# 历史兼容名：受限只读窗口仍引用 WORKSPACE_READ_TOOLS（含 workspace_stat）。
WORKSPACE_READ_TOOLS = WORKSPACE_READ_CAPABILITIES
WORKSPACE_CATALOG_TOOL = "workspace_catalog"

# 审批模式（对应前端“帮我确认”开关）：
#   manual_commit：关闭“帮我确认”——任务中不打断，仅在最终真实写入前确认一次；
#   auto_routine：开启“帮我确认”——普通例行工作区提交也自动完成。
APPROVAL_MODE_CONFIRM = "manual_commit"
APPROVAL_MODE_AUTO = "auto_routine"
_DEFAULT_APPROVAL_MODE = APPROVAL_MODE_CONFIRM  # 未取得 Electron 授权快照时保守处理
ACCESS_LEVEL_FULL = "full"

# 已知/合法的降级码集合（Electron 返回的 error_code 只有在此集合内才
# 会原样透出；其余一律折叠为 WORKSPACE_READ_FAILED，避免下游被任意串污染）。
_KNOWN_STATUS_CODES = frozenset({
    WORKSPACE_NOT_BOUND,
    WORKSPACE_NOT_REGISTERED,
    WORKSPACE_DEVICE_OFFLINE,
    WORKSPACE_ROOT_MISSING,
    WORKSPACE_READ_FAILED,
    "WORKSPACE_DIFF_FAILED",
})

_CACHE_PREFIX = "workspace:ctx"


def _cache_key(workspace_id: str) -> str:
    return f"{_CACHE_PREFIX}:{workspace_id}"


# Electron 工具调用 seam —— 测试时替换为假实现即可，生产路径保持不变。
async def _server_is_healthy(server_name: str) -> bool:
    from app.agents.mcp.manager import ensure_server_healthy

    return await ensure_server_healthy(server_name)


async def _advertised_tools(server_name: str) -> list[str]:
    from app.agents.mcp.manager import list_tools

    return [str(item.get("name") or "") for item in await list_tools(server_name)]


async def _call_electron(
    server_name: str,
    tool_name: str,
    args: dict,
    *,
    task_id: str = "",
    user_id: str = "",
    device_id: str = "",
    workspace_id: str = "",
) -> dict | None:
    from app.agents.mcp.manager import call_tool

    return await call_tool(
        server_name,
        tool_name,
        args,
        task_id=task_id or None,
        user_id=user_id,
        device_id=device_id,
        workspace_id=workspace_id,
    )


# ── WorkspaceContext 模型 ─────────────────────────────────

@dataclass(slots=True)
class WorkspaceContext:
    """工作区当前状态的只读摘要；不含任何文件正文。"""

    workspace_id: str = ""
    name: str = ""
    available: bool = False
    status_code: str = WORKSPACE_NOT_BOUND
    status_message: str = ""
    device_id: str = ""
    server_name: str = ""
    version: int | None = None
    entries: list[dict] = field(default_factory=list)
    staged_changes: list[dict] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    # 执行授权快照（来自 Electron workspace_catalog 的 permission/授权状态；
    # 缺失时保守默认 confirm_on_write）。
    access_level: str = ACCESS_LEVEL_FULL
    approval_mode: str = _DEFAULT_APPROVAL_MODE
    policy_version: str = ""
    issued_at: str = ""
    expires_at: str = ""
    # 按能力分档的工具原始名清单（只读/暂存写/沙箱/提交回滚），
    # 供策略引擎与阶段化工具注入使用；模型不直接消费这里。
    capability_groups: dict = field(default_factory=dict)
    refreshed_at: str = ""

    def to_dict(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "name": self.name,
            "available": self.available,
            "status_code": self.status_code,
            "status_message": self.status_message,
            "device_id": self.device_id,
            "server_name": self.server_name,
            "version": self.version,
            "entries": self.entries,
            "staged_changes": self.staged_changes,
            "capabilities": self.capabilities,
            "access_level": self.access_level,
            "approval_mode": self.approval_mode,
            "policy_version": self.policy_version,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "capability_groups": self.capability_groups,
            "refreshed_at": self.refreshed_at,
        }


_STATUS_MESSAGES: dict[str, str] = {
    WORKSPACE_NOT_BOUND: "当前对话没有绑定工作区；工作区文件无法访问，但不影响不依赖工作区的回答。",
    WORKSPACE_NOT_REGISTERED: "工作区未在后端注册或没有可路由的桌面连接；无法读取其文件。",
    WORKSPACE_DEVICE_OFFLINE: "托管该工作区的桌面设备当前离线；暂时无法读取目录或文件。",
    WORKSPACE_ROOT_MISSING: "工作区根目录不存在或已被删除；不要假装看到文件。",
    WORKSPACE_READ_FAILED: "读取工作区目录失败；不要编造文件内容，可继续回答不依赖工作区的部分。",
    WORKSPACE_READY: "",
}


def describe_status(status_code: str, *, name: str = "", workspace_id: str = "") -> str:
    """给出一条面向模型的、稳定可复用的降级说明。"""
    message = _STATUS_MESSAGES.get(status_code)
    if status_code == WORKSPACE_READY:
        message = "工作区可访问。"
    if message is None:
        message = "工作区当前不可用。"
    head = f"工作区「{name or workspace_id}」" if (name or workspace_id) else "工作区"
    return f"{head}：{message}"


# ── 摘要渲染（Planner / 模型上下文共用，只含目录与状态摘要）──────

def workspace_permission_profile(ctx: WorkspaceContext) -> dict:
    """返回 Planner/审计可消费的执行授权画像（不含任何文件正文）。"""
    return {
        "workspace_id": ctx.workspace_id,
        "workspace_available": ctx.available,
        "access_level": ctx.access_level if ctx.available else "none",
        "approval_mode": ctx.approval_mode,
        "policy_version": ctx.policy_version or "",
        "issued_at": ctx.issued_at or "",
        "expires_at": ctx.expires_at or "",
        "available_domains": ["workspace_read"] + (["workspace_write", "sandbox_execution"] if ctx.available else []),
    }


def workspace_summary_text(ctx: WorkspaceContext) -> str:
    """把 WorkspaceContext 渲染为模型/Planner 可见的中文摘要。

    只暴露根目录条目、条目数量、类型、暂存变化、权限画像与可用能力；
    不暴露深层真实路径、文件内容或实现名称。
    """
    if not ctx.workspace_id:
        return describe_status(WORKSPACE_NOT_BOUND)
    lines = [
        f"当前对话绑定了一个本地工作区「{ctx.name or ctx.workspace_id}」"
        f"（workspace_id={ctx.workspace_id}）。"
    ]
    if not ctx.available:
        lines.append(describe_status(ctx.status_code, name=ctx.name, workspace_id=ctx.workspace_id))
        lines.append("规则：不要假装看到了文件；可以继续完成不依赖工作区的部分；不要要求用户手动填写 workspace_id。")
        return "\n".join(lines)
    if ctx.version is not None:
        lines.append(f"工作区已注册且可访问（当前版本 v{ctx.version}）。")
    else:
        lines.append("工作区已注册且可访问。")
    lines.append("根目录包含：")
    if ctx.entries:
        for entry in ctx.entries:
            path = str(entry.get("path") or entry.get("name") or "?")
            kind = str(entry.get("type") or "file")
            size = entry.get("size")
            suffix = f"（{size} 字节）" if isinstance(size, int) and size else ""
            lines.append(f"- {path}（{'目录' if kind == 'directory' else '文件'}{suffix}）")
    else:
        lines.append("- （空目录，或目录尚未返回条目）")
    if ctx.staged_changes:
        lines.append(f"当前有 {len(ctx.staged_changes)} 项暂存修改。")
    else:
        lines.append("当前没有暂存修改。")
    lines.append(
        "工作区权限：绑定后模型拥有工作区内完整操作能力（读取/搜索/创建/修改/删除/移动/"
        "测试/暂存/提交），但仍不能切换工作区、越出工作区路径、绕过版本与审批，也不能直接修改真实目录——所有写入都经过事务层。"
        f"普通操作是否自动提交由当前执行授权决定（approval_mode={ctx.approval_mode}）；少数高风险操作始终需要确认。"
    )
    read_tools = "、".join(ctx.capabilities) if ctx.capabilities else "（暂不可读取）"
    lines.append(f"可用读取能力：{read_tools}。写入/沙箱/提交类工具由执行策略按阶段注入，不在此窗口常驻。")
    lines.append("规则：只能把上述摘要当作用户授权的工作区状态；不要假装读取摘要之外的任何文件。")
    return "\n".join(lines)


# ── 缓存 ──────────────────────────────────────────────

async def _cache_read(workspace_id: str) -> dict | None:
    try:
        from app.core.redis import get_redis

        raw = await get_redis().get(_cache_key(workspace_id))
    except Exception:  # noqa: BLE001 - 缓存故障不阻塞主流程
        return None
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


async def _cache_write(workspace_id: str, payload: dict) -> None:
    ttl = int(getattr(settings, "WORKSPACE_CONTEXT_CACHE_TTL_SECONDS", 30))
    try:
        from app.core.redis import get_redis

        await get_redis().set(_cache_key(workspace_id), json.dumps(payload, ensure_ascii=False), ex=max(ttl, 5))
    except Exception:  # noqa: BLE001
        pass


async def invalidate_workspace_context(workspace_id: str) -> None:
    """显式失效某个工作区的上下文缓存（提交/回滚/上传/删除后调用）。"""
    try:
        from app.core.redis import get_redis

        await get_redis().delete(_cache_key(workspace_id))
    except Exception:  # noqa: BLE001
        pass


# ── 数据解析 ──────────────────────────────────────────

def _parse_entries(data: Any) -> list[dict]:
    if isinstance(data, dict):
        for key in ("entries", "top_level", "items", "files", "children"):
            value = data.get(key)
            if isinstance(value, list):
                data = value
                break
    if not isinstance(data, list):
        return []
    entries: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        raw_type = str(item.get("type") or item.get("kind") or "")
        if raw_type in {"dir", "directory", "folder"}:
            kind = "directory"
        elif raw_type in {"file"}:
            kind = "file"
        else:
            kind = "directory" if bool(
                item.get("is_directory") or item.get("isDirectory")
                or item.get("is_dir") or item.get("directory")
            ) else "file"
        path = str(item.get("path") or item.get("name") or "").strip().strip("/")
        if not path:
            continue
        entries.append({
            "path": path,
            "type": kind,
            "size": int(item.get("size") or 0) if isinstance(item.get("size"), int) else None,
        })
    return entries


def _parse_version(data: Any, fallback: Any = None) -> int | None:
    for source in (data, fallback):
        if isinstance(source, dict):
            raw = source.get("version")
            if isinstance(raw, dict):  # 防御：不要把整个对象当版本
                raw = source.get("base_version") or source.get("workspace_version")
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
            try:
                return int(str(source.get("base_version") or source.get("workspace_version") or "") or 0)
            except ValueError:
                continue
    return None


def _parse_staged(data: Any) -> list[dict]:
    if isinstance(data, dict):
        value = data.get("staged_changes")
        if value is None:
            value = data.get("staged")
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            paths = value.get("paths")
            if isinstance(paths, list):
                out: list[dict] = []
                for raw in paths:
                    text = str(raw or "")
                    operation, _, path = text.partition(":")
                    if path:
                        out.append({"operation": operation or "change", "path": path})
                    elif text:
                        out.append({"operation": "change", "path": text})
                return out
    return []


# ── 设备/端点解析 ──────────────────────────────────────

def resolve_workspace_desktop(user_id: str, workspace_id: str) -> dict:
    """由 workspace 元数据解析 (server_name, device_id)；不调用网络。

    返回 {"workspace_id", "device_id", "server_name", "status_code"}；
    server_name 为空时表示没有可路由的桌面连接。
    """
    from app.agents.mcp.desktop_connections import desktop_connections

    try:
        meta = workspaces.get_workspace(user_id, workspace_id)
    except LookupError:
        return {
            "workspace_id": workspace_id,
            "device_id": "",
            "server_name": "",
            "status_code": WORKSPACE_NOT_REGISTERED,
        }
    device_id = str(meta.get("device_id") or "")
    server_name = str(meta.get("device_server") or "")
    endpoint = None
    if server_name:
        endpoint = desktop_connections.resolve(
            server_name, user_id=user_id, device_id=device_id
        )
    if endpoint is None:
        endpoint = desktop_connections.resolve_desktop(
            user_id=user_id, device_id=device_id, preferred_name=server_name
        )
    if endpoint is not None:
        server_name = endpoint.name
        device_id = device_id or endpoint.device_id
        return {
            "workspace_id": workspace_id,
            "device_id": device_id,
            "server_name": server_name,
            "status_code": WORKSPACE_READY,
        }
    return {
        "workspace_id": workspace_id,
        "device_id": device_id,
        "server_name": "",
        "status_code": WORKSPACE_NOT_REGISTERED,
    }


# ── 主入口：加载工作区上下文 ─────────────────────────────

async def load_workspace_context(
    user_id: str,
    *,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    device_id: str = "",
    force_refresh: bool = False,
) -> WorkspaceContext:
    """解析并加载工作区上下文（读缓存 → 版本探测 → catalog/回退刷新）。

    该函数只读、无副作用；失败时返回带降级状态码的 WorkspaceContext，
    绝不抛异常（调用方用 ``available`` / ``status_code`` 分支）。
    """
    # 1) conversation -> workspace 解析
    wsid = str(workspace_id or "").strip()
    name = ""
    if not wsid and conversation_id:
        meta = workspaces.workspace_for_conversation(user_id, conversation_id)
        if meta is None:
            return WorkspaceContext(status_code=WORKSPACE_NOT_BOUND)
        wsid = str(meta.get("workspace_id") or "")
        name = str(meta.get("name") or "")
    if not wsid:
        return WorkspaceContext(status_code=WORKSPACE_NOT_BOUND)
    if not name:
        try:
            meta = workspaces.get_workspace(user_id, wsid)
            name = str(meta.get("name") or "")
        except LookupError:
            pass

    if force_refresh:
        await invalidate_workspace_context(wsid)

    # 2) 设备/端点解析
    route = resolve_workspace_desktop(user_id, wsid)
    if route["status_code"] != WORKSPACE_READY:
        return WorkspaceContext(
            workspace_id=wsid, name=name,
            available=False, status_code=route["status_code"],
            status_message=describe_status(route["status_code"], name=name, workspace_id=wsid),
        )
    server_name = route["server_name"]
    device_id = route["device_id"] or device_id
    if not await _server_is_healthy(server_name):
        return WorkspaceContext(
            workspace_id=wsid, name=name,
            available=False, status_code=WORKSPACE_DEVICE_OFFLINE,
            status_message=describe_status(WORKSPACE_DEVICE_OFFLINE, name=name, workspace_id=wsid),
            device_id=device_id, server_name=server_name,
        )

    # 3) 缓存命中（新鲜度以内直接返回）
    now = time.time()
    cached = await _cache_read(wsid)
    probe_ttl = float(getattr(settings, "WORKSPACE_VERSION_PROBE_TTL_SECONDS", 10))
    if cached and isinstance(cached, dict) and cached.get("version") is not None:
        age = now - float(cached.get("ts") or 0)
        if age < probe_ttl or age < float(getattr(settings, "WORKSPACE_CONTEXT_CACHE_TTL_SECONDS", 30)):
            return _context_from_cache(cached, name=name, server_name=server_name, device_id=device_id, workspace_id=wsid)

    # 4) 版本探测（轻量 diff），决定是否需要重建
    base_version: int | None = None
    diff_data: dict | None = None
    diff = await _call_electron(
        server_name, "workspace_diff",
        {"workspace_id": wsid},
        user_id=user_id, device_id=device_id, workspace_id=wsid,
    )
    if diff is not None and isinstance(diff, dict):
        diff_data = diff.get("data") if isinstance(diff.get("data"), dict) else {}
        base_version = _parse_version(diff_data, diff)
    if cached and isinstance(cached, dict) and cached.get("version") is not None:
        if base_version is None:
            # 版本探测失败但缓存完整 → 继续用缓存，不让一次抖动破坏可读性。
            return _context_from_cache(cached, name=name, server_name=server_name, device_id=device_id, workspace_id=wsid)
        if int(cached["version"]) == int(base_version):
            cached["ts"] = now
            await _cache_write(wsid, cached)
            return _context_from_cache(cached, name=name, server_name=server_name, device_id=device_id, workspace_id=wsid)

    # 5) 全量目录刷新：workspace_catalog，缺失时回退 workspace_list。
    tool_args = {"workspace_id": wsid}
    advertised = await _advertised_tools(server_name)
    if WORKSPACE_CATALOG_TOOL in advertised:
        result = await _call_electron(
            server_name, WORKSPACE_CATALOG_TOOL, tool_args,
            user_id=user_id, device_id=device_id, workspace_id=wsid,
        )
    else:
        result = await _call_electron(
            server_name, "workspace_list", {**tool_args, "path": ""},
            user_id=user_id, device_id=device_id, workspace_id=wsid,
        )
    status_code, status_message = WORKSPACE_READY, ""
    if result is None:
        status_code = WORKSPACE_READ_FAILED
        status_message = describe_status(status_code, name=name, workspace_id=wsid)
    elif isinstance(result, dict) and result.get("status") in {"failed", "cancelled"}:
        status_code = str(result.get("error_code") or "") or WORKSPACE_READ_FAILED
        if status_code not in _KNOWN_STATUS_CODES:
            status_code = WORKSPACE_READ_FAILED
        status_message = describe_status(status_code, name=name, workspace_id=wsid)
    data = result.get("data") if isinstance(result, dict) else None
    entries = _parse_entries(data)
    staged = _parse_staged(data) or _parse_staged(diff_data) or []
    catalog_version = _parse_version(data if isinstance(data, dict) else None, diff_data)
    version = catalog_version if catalog_version is not None else base_version
    advertised_set = set(advertised)
    capability_groups = {
        "read": sorted(WORKSPACE_READ_CAPABILITIES & advertised_set),
        "stage_write": sorted(WORKSPACE_STAGE_WRITE_CAPABILITIES & advertised_set),
        "sandbox": sorted(WORKSPACE_SANDBOX_CAPABILITIES & advertised_set),
        "commit": sorted(WORKSPACE_COMMIT_CAPABILITIES & advertised_set),
    }
    capabilities = capability_groups["read"]
    permission = _parse_permission(data)
    ctx = WorkspaceContext(
        workspace_id=wsid, name=name,
        available=status_code == WORKSPACE_READY,
        status_code=status_code, status_message=status_message,
        device_id=device_id, server_name=server_name,
        version=version, entries=entries,
        staged_changes=staged, capabilities=capabilities,
        approval_mode=permission["approval_mode"],
        policy_version=permission["policy_version"],
        issued_at=permission["issued_at"],
        expires_at=permission["expires_at"],
        capability_groups=capability_groups,
        refreshed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    await _cache_write(wsid, {
        "ts": now,
        "workspace_id": wsid,
        "version": ctx.version,
        "entries": ctx.entries,
        "staged_changes": ctx.staged_changes,
        "capabilities": ctx.capabilities,
        "approval_mode": ctx.approval_mode,
        "policy_version": ctx.policy_version,
        "issued_at": ctx.issued_at,
        "expires_at": ctx.expires_at,
        "capability_groups": ctx.capability_groups,
    })
    return ctx


def _context_from_cache(cached: dict, *, name: str, server_name: str, device_id: str,
                        workspace_id: str = "") -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id or str(cached.get("workspace_id") or ""),
        name=name,
        available=True,
        status_code=WORKSPACE_READY,
        device_id=device_id,
        server_name=server_name,
        version=cached.get("version"),
        entries=[dict(item) for item in (cached.get("entries") or []) if isinstance(item, dict)],
        staged_changes=[dict(item) for item in (cached.get("staged_changes") or []) if isinstance(item, dict)],
        capabilities=[str(item) for item in (cached.get("capabilities") or []) if str(item)],
        approval_mode=str(cached.get("approval_mode") or _DEFAULT_APPROVAL_MODE),
        policy_version=str(cached.get("policy_version") or ""),
        issued_at=str(cached.get("issued_at") or ""),
        expires_at=str(cached.get("expires_at") or ""),
        capability_groups=cached.get("capability_groups")
        if isinstance(cached.get("capability_groups"), dict) else {},
    )


def _parse_permission(data: Any) -> dict:
    """从 workspace_catalog 结果解析执行授权快照（保守默认 manual_commit）。"""
    if not isinstance(data, dict):
        return {
            "approval_mode": _DEFAULT_APPROVAL_MODE,
            "policy_version": "", "issued_at": "", "expires_at": "",
        }
    # Electron 的正式字段是 permission_profile；permission 保留给早期客户端。
    if isinstance(data.get("permission_profile"), dict):
        source = data["permission_profile"]
    elif isinstance(data.get("permission"), dict):
        source = data["permission"]
    else:
        source = data
    raw_mode = str(source.get("approval_mode") or "").strip()
    # 接受上一版后端内部名，统一投影为前后端协议名 manual_commit。
    if raw_mode == "confirm_on_write":
        raw_mode = APPROVAL_MODE_CONFIRM
    if raw_mode not in {APPROVAL_MODE_AUTO, APPROVAL_MODE_CONFIRM}:
        raw_mode = _DEFAULT_APPROVAL_MODE
    return {
        "approval_mode": raw_mode,
        "policy_version": str(source.get("policy_version") or "")[:40],
        "issued_at": str(source.get("issued_at") or "")[:64],
        "expires_at": str(source.get("expires_at") or "")[:64],
    }
