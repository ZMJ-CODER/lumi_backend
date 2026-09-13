"""Workflow Skill 的能力声明与工具解析（方案《资源能力层》Phase 4）。

## 迁移的形状

```text
改造前：allowed_tools = [
           "mcp__lumi_client__workspace_navigator", "…workspace_write",
           "…workspace_edit", "…sandbox_run", … 14 个底层名字 ]

改造后：required_capabilities = [
           "resource.read", "resource.write", "resource.edit",
           "resource.move", "resource.delete", "code.execute" ]
        resource_types = ["workspace"]
        providers      = ["workspace_provider"]

        底层工具名由 Provider Adapter（Phase 3）解析，Workflow 内部照旧按
        读取 → 修改 → 测试 → 提交 执行，每一次调用仍走统一执行器（授权/审计/
        审批/副作用日志/互斥锁全部不变）。
```

## 兼容策略（一个版本周期）

* 旧 Skill 只声明 ``allowed_tools``：``legacy_tools_to_capabilities()`` 把它们**当成**
  能力声明来读，于是能力派生的工具行与旧行**并存**（只补不替）；
* 旧 Workflow Skill 不迁移也能跑：声明为空时 ``effective_capabilities()`` 从
  ``allowed_tools`` 推导，行为与改造前一致；
* 新 Skill 只声明能力即可：``tools_for_capabilities()`` 负责落到具体工具名。

## 为什么"只补不替"

如果能力派生**替换**了旧工具行，`resolve_dependencies` 看到的依赖会变少，
于是"迁移到能力声明"就变成了"依赖检查变松"——那是回归，不是迁移。
因此这里返回的是**并集**，Phase 5 收敛模型工具面时才会收紧。
"""

from __future__ import annotations

from typing import Any, Iterable

from loguru import logger

from app.agents.capabilities.resource_catalog import (
    UNIFIED_CAPABILITIES,
    binding_for_tool,
    normalize_unified_capability,
)


def workflow_enabled() -> bool:
    """``RESOURCE_CAPABILITY_WORKFLOW``（默认关闭：关闭时旧声明路径逐字不变）。"""
    try:
        from app.core.feature_flags import feature_enabled

        return feature_enabled("RESOURCE_CAPABILITY_WORKFLOW")
    except Exception:  # noqa: BLE001
        return False


def legacy_tools_to_capabilities(tools: Iterable[str]) -> list[str]:
    """旧 MCP 工具名 → 统一能力（**兼容解析**：老依赖不改一行也能被能力层理解）。"""
    out: list[str] = []
    for name in tools or ():
        binding = binding_for_tool(str(name or ""))
        if binding.known and binding.capability not in out:
            out.append(binding.capability)
    return out


def tools_for_capabilities(
    capabilities: Iterable[str],
    resource_types: Iterable[str] = (),
) -> list[str]:
    """能力声明 → 具体工具名（经 Provider Adapter；解析不出的能力**不编名字**）。

    资源类型为空时不做解析：``resource.write`` 跨多种资源，硬挑一个会给出错误的工具。
    """
    from app.agents.capabilities.resource_dispatch import adapter_tool_for

    resources = [str(item or "").strip() for item in resource_types if str(item or "").strip()]
    out: list[str] = []
    if not resources:
        return out
    for raw in capabilities or ():
        capability = normalize_unified_capability(str(raw or "").strip())
        if capability not in UNIFIED_CAPABILITIES:
            continue
        for resource_type in resources:
            tool = adapter_tool_for(capability, resource_type)
            if tool and tool not in out:
                out.append(tool)
    return out


def declared_declarations(skill: Any) -> dict[str, Any]:
    """Skill 的**能力声明视图**（落 Job 快照 / API / 排障；不含参数与正文）。"""
    getter = getattr(skill, "capability_dependencies", None)
    if callable(getter):
        try:
            view = dict(getter() or {})
        except Exception as exc:  # noqa: BLE001 - 声明读取失败不能让 Skill 不可用
            logger.debug("[resource-workflow] 能力声明读取失败: {}", str(exc)[:120])
            view = {}
    else:
        view = {}
    tools = [str(item or "") for item in (getattr(skill, "allowed_tools", None) or ())]
    if tools and not view.get("capabilities"):
        view["capabilities"] = legacy_tools_to_capabilities(tools)
    view.setdefault("resource_types", [])
    view.setdefault("providers", [])
    view.setdefault("declared", False)
    view["tools"] = tools
    return view


def select_capabilities(
    items: Iterable[Any],
    *,
    capabilities: Iterable[str] = (),
    resource_types: Iterable[str] = (),
    legacy_names: Iterable[str] = (),
) -> list[Any]:
    """按**能力声明**筛选运行期能力（替代"照着 14 个底层名字过滤"）。

    ``legacy_names`` 是旧名字白名单：命中即保留。两者取**并集**——
    与依赖声明同一条原则（迁移期只补不替），这样即使某个 Provider 的绑定暂时
    没登记，旧名字仍然能选中它。
    """
    wanted = {normalize_unified_capability(str(item or "").strip()) for item in capabilities or ()}
    resources = {str(item or "").strip() for item in resource_types or ()}
    legacy = {str(item or "").strip() for item in legacy_names or ()}
    out: list[Any] = []
    for item in items or ():
        name = str(getattr(item, "name", "") or "")
        if not name:
            continue
        if name in legacy:
            out.append(item)
            continue
        binding = binding_for_tool(name)
        if not binding.known:
            continue
        if wanted and binding.capability not in wanted:
            continue
        if resources and binding.resource_type not in resources:
            continue
        if not wanted and not resources:
            continue
        out.append(item)
    return out


def workflow_capability_dependencies(skill: Any) -> dict[str, Any]:
    """给依赖检查用的**能力型 manifest 片段**（Phase 4 的对外形状）。"""
    view = declared_declarations(skill)
    return {
        "capabilities": list(view.get("capabilities") or []),
        "resource_types": list(view.get("resource_types") or []),
        "providers": list(view.get("providers") or []),
        "declared": bool(view.get("declared")),
    }


__all__ = [
    "declared_declarations",
    "legacy_tools_to_capabilities",
    "select_capabilities",
    "tools_for_capabilities",
    "workflow_capability_dependencies",
    "workflow_enabled",
]
