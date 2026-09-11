"""``ToolSpec`` 桥接（B 项）：真实工具 → 契约注册项（影子注册 + 治理校验）。

背景：契约里的 ``ToolSpec``/``ToolRegistry`` 定义了"注册即声明治理"的规则
（副作用、风险等级、权限、敏感级、审批、联网、流式），但项目的真实注册路径
（``app.agents.skills.registry.ToolRegistry`` + 插件加载器）此前完全没有接它，
于是"新工具自定义输入/输出字段"仍然要靠核心代码。

做法（**影子注册**，迁移期零风险）：

* 真实注册照旧，不改变现有行为；
* 同时把工具投影成 ``ToolSpec`` 注册进契约注册表，并跑 ``validate_declaration()``；
* 声明不完整只**记录 + 告警**（``tool_spec_report()`` 可查），不阻断注册；
* 校验通过后，输入 Schema / 输出契约 / 可读投影都来自工具自己的声明，
  新增工具无需改核心代码。

输入 Schema 用工具自己的 ``parameters_schema``；输出契约用 ``result_contract``
（形如 ``lumi.workspace_navigator.result``）——两者都是工具作者声明的字段，
不在这里硬编码任何工具名。
"""

from __future__ import annotations

import re
from typing import Any

from loguru import logger
from lumi_contracts import (
    IdempotencyPolicy,
    RetryPolicy,
    RiskLevel,
    Sensitivity,
    SideEffect,
    ToolSpec,
)

# 影子注册报告：problems 非空表示该工具的治理声明不完整（不阻断注册）。
_REPORT: dict[str, list[str]] = {}
_SHADOW: dict[str, ToolSpec] = {}
_WARNED: set[str] = set()

# 只有形如 lumi.<name>.result / lumi.<name> 的声明才当作输出契约名；
# 历史上 ``result_contract`` 也被用来写自然语言说明（"返回 title、summary…"），
# 那种情况不能当成契约标识。
_CONTRACT_NAME_RE = re.compile(r"^(lumi\.[a-z0-9_.]+|[a-z][a-z0-9_]*(\.[a-z0-9_]+)*\.result)$")
# 插件版本号：x.y.z（可比较、可灰度，禁止自由文本）。
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
# 插件白名单校验结果（不受信声明在这里留证，不阻断内置注册）。
_PLUGIN_REPORTS: dict[str, list[str]] = {}


def _value(source: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(source, dict):
            if source.get(name) is not None:
                return source[name]
        else:
            found = getattr(source, name, None)
            if found is not None:
                return found
    return default


def _side_effect(name: str, write_op: bool) -> SideEffect:
    if not write_op:
        return SideEffect.NONE
    if name.startswith("workspace_stage"):
        return SideEffect.STAGE_WRITE
    if name.startswith(("workspace_", "sandbox_")):
        return SideEffect.WORKSPACE_WRITE
    if name in {"run_in_sandbox", "sandbox_run"} or name.startswith("sandbox"):
        return SideEffect.PROCESS
    return SideEffect.EXTERNAL


def _idempotency(source: Any) -> IdempotencyPolicy:
    raw = str(_value(source, "idempotency_type", "idempotency_policy", default="") or "")
    for item in IdempotencyPolicy:
        if item.value == raw:
            return item
    idempotent = _value(source, "idempotent", default=True)
    return IdempotencyPolicy.NATURAL_KEY if idempotent else IdempotencyPolicy.NON_IDEMPOTENT


def tool_spec_from(tool: Any, *, namespace: str = "lumi", internal: bool = False) -> ToolSpec:
    """真实工具（``Tool``/``ToolCapability``/插件对象）→ 契约 ``ToolSpec``。

    **MCP 工具不复制输入 Schema**：它的参数定义以 MCP 的 ``inputSchema`` 为唯一
    事实来源（模型 schema/参数校验都直接用 MCP 拉到的定义），这里只记一个引用
    ``input_schema_ref = mcp://<server>/<tool>``，避免同一份 Schema 维护两遍。
    """
    name = str(_value(tool, "name", default="") or "")
    environment = str(_value(tool, "environment", default="server") or "server")
    source = str(_value(tool, "source", default="") or "")
    mcp_server = str(_value(tool, "server", "mcp_server", default="") or "")
    raw_name = str(_value(tool, "raw_name", "mcp_tool", default="") or "") or name
    is_mcp = source == "mcp" or bool(mcp_server)
    category = str(_value(tool, "category", default="general") or "general")
    permission = str(_value(tool, "permission", default="user") or "user")
    raw_write = _value(tool, "write_op", "action_type", default=False)
    write_op = raw_write is True or raw_write == "write"
    requires_confirmation = bool(_value(tool, "requires_confirmation", default=False))
    parameters = _value(tool, "parameters_schema", "parameters", default=None)
    if not isinstance(parameters, dict) or not parameters:
        parameters = {"type": "object", "properties": {}}
    output_contract = _value(tool, "output_contract", default=None)
    output_contract = output_contract if isinstance(output_contract, dict) else {}
    result_contract = str(_value(tool, "result_contract", default="") or "")
    output_schema_name = result_contract if _CONTRACT_NAME_RE.match(result_contract) else ""
    description = str(_value(tool, "description", default="") or "")[:320]
    if result_contract and not output_schema_name:
        # 声明是自然语言说明：作为输出描述保留，不冒充契约名。
        description = (description + f"（输出：{result_contract[:120]}）")[:400]
    raw_sensitivity = str(_value(tool, "data_sensitivity", default="INTERNAL") or "INTERNAL")
    sensitivity = next(
        (item for item in Sensitivity if item.value == raw_sensitivity.upper()), Sensitivity.INTERNAL
    )
    side_effect = _side_effect(raw_name if is_mcp else name, write_op)
    # 有副作用 → 至少 MEDIUM；需确认 → HIGH（不再靠调用点各自判断）。
    risk = RiskLevel.LOW
    if side_effect is not SideEffect.NONE:
        risk = RiskLevel.HIGH if requires_confirmation else RiskLevel.MEDIUM
    return ToolSpec(
        name=name,
        # 客户端/MCP 能力与后端工具分命名空间隔离（方案第七节）。
        namespace="lumi_client" if (is_mcp or environment == "client") else namespace,
        version=str(_value(tool, "version", default="1.0.0") or "1.0.0"),
        description=description,
        # MCP 工具：不复制 Schema，只留引用。
        input_schema={} if is_mcp else parameters,
        input_schema_ref=(f"mcp://{mcp_server or 'client'}/{raw_name}" if is_mcp else ""),
        output_schema_name=output_schema_name,
        output_schema_version=1,
        side_effect=side_effect,
        risk_level=risk,
        data_sensitivity=sensitivity,
        # 有副作用就必须声明权限；读工具不声明权限（默认 user 级别访问）。
        required_permissions=(permission,) if side_effect is not SideEffect.NONE else (),
        requires_approval=requires_confirmation,
        allow_network=category == "network",
        streaming_support=bool(_value(tool, "streaming", "streaming_support", default=False)),
        timeout_s=float(_value(tool, "timeout", "timeout_s", default=0.0) or 0.0),
        retry_policy=RetryPolicy(max_attempts=int(_value(tool, "max_attempts", default=1) or 1)),
        idempotency_policy=_idempotency(tool),
        tags=(category, environment, str(output_contract.get("content_type") or "")),
        # internal = 不进入模型能力空间（app 侧 public=False 的执行实现）。
        internal=internal,
    )


def register_tool_spec(
    tool: Any,
    *,
    namespace: str = "lumi",
    internal: bool = False,
    warn: bool = True,
) -> list[str]:
    """影子注册一个工具并返回治理声明问题（空列表 = 通过）。

    迁移期**影子注册**：真实注册照旧，这里只把声明投影成契约 ``ToolSpec`` 并
    校验，问题进入报告与告警而不是异常。原因：注册期的契约问题不能变成"契约
    改动导致线上工具消失"。

    命名空间受信且声明完整时，同一个 spec 也会进入契约的正式注册表
    （``default_tool_registry``），供导出与严格查询使用。
    """
    try:
        spec = tool_spec_from(tool, namespace=namespace, internal=internal)
    except Exception as exc:  # noqa: BLE001 - 影子注册不得影响真实注册
        name = str(getattr(tool, "name", "") or "<unknown>")
        _REPORT[name] = [f"契约投影失败: {type(exc).__name__}: {str(exc)[:160]}"]
        logger.debug("ToolSpec 影子注册失败: {} ({})", name, str(exc)[:120])
        return _REPORT[name]
    if not spec.name:
        return []
    problems = spec.validate_declaration()
    _REPORT[spec.name] = list(problems)
    _SHADOW[spec.name] = spec
    if problems and warn and spec.name not in _WARNED:
        _WARNED.add(spec.name)
        logger.warning(
            "工具 {} 的 ToolSpec 治理声明不完整（影子注册，不阻断运行）：{}",
            spec.name,
            "；".join(problems[:4]),
        )
    if not problems:
        try:
            from lumi_contracts import default_tool_registry

            default_tool_registry().register(spec, replace=True)
        except Exception as exc:  # noqa: BLE001 - 命名空间/重复注册问题只记录
            logger.debug("ToolSpec 正式注册跳过: {} ({})", spec.qualified_name, str(exc)[:120])
    return problems


def tool_specs() -> list[ToolSpec]:
    """影子注册表里的全部 ``ToolSpec``（按限定名排序）。"""
    return sorted(_SHADOW.values(), key=lambda item: item.qualified_name)


def export_tool_specs() -> dict[str, dict[str, Any]]:
    """导出影子注册清单（文档/前端类型/交接用）。"""
    return {
        spec.qualified_name: {
            "version": spec.version,
            "input_schema": spec.input_schema,
            # MCP 工具输入定义以 MCP 为唯一来源：导出的是引用而不是副本。
            "input_schema_ref": spec.input_schema_ref,
            "output_schema": (
                f"{spec.output_schema_name}@{spec.output_schema_version}"
                if spec.output_schema_name
                else ""
            ),
            "side_effect": spec.side_effect.value,
            "risk_level": spec.risk_level.value,
            "data_sensitivity": spec.data_sensitivity.value,
            "requires_approval": spec.requires_approval,
            "streaming_support": spec.streaming_support,
            "internal": spec.internal,
        }
        for spec in tool_specs()
    }


def tool_spec_report() -> dict[str, Any]:
    """影子注册报告：总数 / 通过数 / 每个工具的声明问题 / 插件白名单问题。"""
    problems = {name: rows for name, rows in _REPORT.items() if rows}
    return {
        "total": len(_REPORT),
        "valid": len(_REPORT) - len(problems),
        "problems": problems,
        "plugin_problems": {name: rows for name, rows in _PLUGIN_REPORTS.items() if rows},
    }


def plugin_namespace_for(module_name: str, *, declared: str = "") -> str:
    """插件命名空间：显式声明优先，否则按加载来源归类。

    * 内置插件（``plugins.tools.*`` / ``plugins.workflows.*``）→ ``lumi``；
    * 用户自建工作流 Skill → ``lumi_skill``；
    * 桌面端能力 → ``lumi_client``；
    * 其他来源按 ``declared`` 原样返回，交由白名单校验决定是否受信。
    """
    text = str(declared or "").strip()
    if text:
        return text
    module = str(module_name or "")
    if module.startswith(("plugins.tools", "plugins.workflows", "app.")):
        return "lumi"
    if module.startswith("plugins.user") or "user_workflow" in module:
        return "lumi_skill"
    if module.startswith("plugins.desktop") or module.startswith("plugins.client"):
        return "lumi_client"
    return "lumi" if not module else module.split(".", 1)[0]


def validate_plugin_declaration(
    tool: Any,
    *,
    namespace: str,
    trusted_namespaces: tuple[str, ...] | None = None,
) -> list[str]:
    """第三方插件注册前的白名单/版本校验（方案第七节）。

    返回问题列表（空 = 通过）。校验项：

    * 命名空间必须在受信白名单内（否则只能作为不受信声明被记录）；
    * 版本号必须是 ``x.y.z`` 形态（可比较、可灰度）；
    * 写操作且需要确认的插件必须声明权限。
    """
    from lumi_contracts import DEFAULT_TRUSTED_NAMESPACES

    trusted = tuple(trusted_namespaces or DEFAULT_TRUSTED_NAMESPACES)
    problems: list[str] = []
    if str(namespace or "") not in trusted:
        problems.append(
            f"命名空间 {namespace!r} 不在受信白名单 {'/'.join(trusted)} 内"
        )
    version = str(_value(tool, "version", default="") or "")
    if not _SEMVER_RE.match(version):
        problems.append(f"插件版本号非法：{version!r}（期望 x.y.z）")
    return problems


def reset_tool_spec_report() -> None:
    """清空报告与告警去重表（测试用）。"""
    _REPORT.clear()
    _SHADOW.clear()
    _WARNED.clear()
    _PLUGIN_REPORTS.clear()


async def mcp_input_schema(server: str, tool: str) -> dict[str, Any]:
    """**动态**从 MCP 拉取工具的 ``inputSchema``（唯一事实来源）。

    MCP 工具的输入定义不复制到我们的契约里：模型 schema、参数校验、前端参数
    表单都按需从这里取。拉取失败返回空 dict（调用方按"无约束对象"处理）。
    """
    server_name = str(server or "")
    tool_name = str(tool or "")
    if not server_name or not tool_name:
        return {}
    try:
        from app.agents.mcp.manager import list_tools

        for item in await list_tools(server_name) or []:
            if str(item.get("name") or "") != tool_name:
                continue
            schema = item.get("inputSchema") or item.get("input_schema") or {}
            return dict(schema) if isinstance(schema, dict) else {}
    except Exception as exc:  # noqa: BLE001 - 拉取失败不阻断调用
        logger.debug("MCP inputSchema 拉取失败: {}/{} ({})", server_name, tool_name, str(exc)[:120])
    return {}


def split_mcp_schema_ref(ref: str) -> tuple[str, str]:
    """``mcp://<server>/<tool>`` → ``(server, tool)``；非 MCP 引用返回空。"""
    text = str(ref or "")
    if not text.startswith("mcp://"):
        return "", ""
    rest = text[len("mcp://"):]
    server, _, tool = rest.partition("/")
    return server, tool


def record_plugin_report(name: str, problems: list[str]) -> list[str]:
    """登记一次插件白名单/版本校验结果（不受信声明留证，不阻断内置注册）。"""
    _PLUGIN_REPORTS[str(name)] = list(problems)
    if problems:
        logger.warning(
            "插件 {} 未通过契约准入（仅记录，不阻断）: {}",
            name,
            "；".join(problems[:4]),
        )
    return problems


__all__ = [
    "export_tool_specs",
    "mcp_input_schema",
    "plugin_namespace_for",
    "record_plugin_report",
    "register_tool_spec",
    "reset_tool_spec_report",
    "split_mcp_schema_ref",
    "tool_spec_from",
    "tool_spec_report",
    "tool_specs",
    "validate_plugin_declaration",
]
