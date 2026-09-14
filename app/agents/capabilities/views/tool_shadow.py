"""工具注册表的**影子对拍**（四分类中的"诊断投影"，只出报告、不参与决策）。

这一段回答的是**切换真相源之前必须
看的那个问题**：把静态表换成注册表派生之后，行为会不会变？

* 四个**判定维度**（``SHADOW_PARITY_DIMENSIONS``）必须逐条一致，``switch_safe`` 才为真；
* 四个**披露维度**（``SHADOW_DECLARED_DIMENSIONS``）是**有意差异**（声明带来的档位/窗口补位、
  资源层窗口只增不减、模型可见面收敛只减），只摊开给人看，不参与判定。

三个容易踩的坑（都在代码注释里写明了）：

1. **缺维度 ≠ 没差异**：某维度算失败时键会缺失，``shadow_parity_totals`` 把缺失一律判为
   不安全——否则"没跑成"会被读成"一致"；
2. **基线必须是纯静态词表**（``static_tier_of``），不能用已经混入派生的
   ``classify_tool_risk``，否则对拍永远显示一致，等于没对拍；
3. **披露行必须与判定行同为三格**（``[名字, 静态值, 派生值]``）：前端按统一形状渲染。

依赖方向：本模块 → ``catalog.tool_registry``（**只在函数内 import**，避免模块级环）。
反向依赖是不允许的：注册表不该知道影子对拍的存在。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

# ── [诊断投影] 静态 vs 派生影子对拍（只出报告，不参与决策）
SHADOW_PARITY_DIMENSIONS: tuple[str, ...] = (
    "tool→capability",
    "capability→mcp_target",
    "intent→tool_window",
    "tool→risk_tier",
)

#: 仅作披露的维度（声明带来的有意差异）。
SHADOW_DECLARED_DIMENSION = "tool→risk_tier(declared)"
#: 声明带来的动作窗口补位（静态表没有这个能力，属于有意差异）。
SHADOW_DECLARED_WINDOW_DIMENSION = "intent→tool_window(declared)"
#: 统一资源能力层的窗口差异（Phase 2）：**只增不减**，同属"有意差异"。
SHADOW_RESOURCE_WINDOW_DIMENSION = "intent→resource_window(resource layer)"
#: 模型可见面收敛差异（Phase 5）：**只减**（实现层名字不再直接暴露），同属"有意差异"。
SHADOW_MODEL_SURFACE_DIMENSION = "tool→model_surface(converged)"

#: 全部**仅披露**维度（有意差异，不参与 ``switch_safe``）。
#:
#: 单一事实源：管理端接口、前端与测试都读它。新增一个披露维度只需改这一处——
#: 之前每加一维都要同时改两个测试里的硬编码集合，那种"漏改就红"的摩擦本身就是缺陷。
SHADOW_DECLARED_DIMENSIONS: tuple[str, ...] = (
    SHADOW_DECLARED_DIMENSION,
    SHADOW_DECLARED_WINDOW_DIMENSION,
    SHADOW_RESOURCE_WINDOW_DIMENSION,
    SHADOW_MODEL_SURFACE_DIMENSION,
)


def shadow_parity_totals(
    diffs: dict[str, list[list[str]]] | None = None,
) -> dict[str, Any]:
    """把影子对比结果折成"能不能切"的结论。

    **缺维度 ≠ 没差异**：某一维度计算失败时键会缺失，若按"没有非空列表"来判断，
    会把"没跑成"读成"一致"。因此缺失的维度一律视为不安全。
    """
    rows = shadow_compare() if diffs is None else diffs
    missing = [name for name in SHADOW_PARITY_DIMENSIONS if name not in rows]
    parity_total = sum(len(rows.get(name) or []) for name in SHADOW_PARITY_DIMENSIONS)
    declared_total = sum(len(rows.get(name) or []) for name in SHADOW_DECLARED_DIMENSIONS)
    return {
        "parity_total": parity_total,
        "declared_total": declared_total,
        "missing_dimensions": missing,
        "switch_safe": not missing and parity_total == 0,
    }


def shadow_compare() -> dict[str, list[list[str]]]:
    """对比"静态表"与"派生结果"的差异，返回 ``{维度: [[静态, 派生], ...]}``。

    只读、无副作用；可在任务提交时打点，也可由管理接口按需调用。差异列表是判断
    "能不能把真相源切到注册表"的唯一依据——差异为空的维度才能安全切换。
    """
    from app.agents.capabilities.catalog.tool_registry import (
        action_window_declared,
        action_window_static,
        declared_tier_of,
        entries_by_name,
        mcp_target_for,
        risk_tier_of,
    )

    diffs: dict[str, list[list[str]]] = {}

    # 维度 1：工具 → 能力
    try:
        from app.agents.capabilities.registry.builtin import TOOL_CAPABILITY_MAP

        rows: list[list[str]] = []
        derived = entries_by_name()
        for tool, static_cap in sorted(TOOL_CAPABILITY_MAP.items()):
            entry = derived.get(tool)
            derived_cap = entry.capability if entry is not None else ""
            if str(static_cap or "") != str(derived_cap or ""):
                rows.append([tool, str(static_cap or ""), str(derived_cap or "")])
        diffs["tool→capability"] = rows
    except Exception as exc:  # noqa: BLE001
        logger.debug("[tool-registry] 影子对比（工具→能力）失败: {}", str(exc)[:120])

    # 维度 2：能力 → MCP 目标
    try:
        from app.agents.capabilities.broker.dispatch import CAPABILITY_TOOL_MAP

        rows = []
        for capability, static_tool in sorted(CAPABILITY_TOOL_MAP.items()):
            derived_tool = mcp_target_for(capability)
            if str(static_tool or "") != str(derived_tool or ""):
                rows.append([capability, str(static_tool or ""), str(derived_tool or "")])
        diffs["capability→mcp_target"] = rows
    except Exception as exc:  # noqa: BLE001
        logger.debug("[tool-registry] 影子对比（能力→MCP）失败: {}", str(exc)[:120])

    # 维度 3：动作意图 → 工具窗口
    try:
        from app.agents.orchestration.preflight.capability_preflight import ACTION_TOOL_WINDOW

        rows = []
        declared_rows = []
        for intent, static_tools in sorted(ACTION_TOOL_WINDOW.items()):
            derived_tools = action_window_static(intent, fallback=tuple(static_tools))
            if sorted(static_tools) != sorted(derived_tools):
                rows.append([intent, ",".join(static_tools), ",".join(derived_tools)])
            additions = action_window_declared(intent, fallback=tuple(static_tools))
            if additions:
                # 声明带来的补位：静态表根本没有这个能力，属于有意差异（不计入
                # switch_safe），与档位声明同一个处理口径。
                declared_rows.append([intent, ",".join(static_tools), ",".join(additions)])
        diffs["intent→tool_window"] = rows
        if declared_rows:
            diffs[SHADOW_DECLARED_WINDOW_DIMENSION] = declared_rows
        # 统一资源能力层（Phase 2）：静态窗口 vs 资源层窗口。差异**全是新增**
        # （资源层从不删工具），因此与"声明维度"同口径披露，不参与 switch_safe。
        try:
            from app.agents.capabilities.policy.resource_window import shadow_compare_windows

            resource_rows = shadow_compare_windows(ACTION_TOOL_WINDOW)
            if resource_rows:
                diffs[SHADOW_RESOURCE_WINDOW_DIMENSION] = resource_rows
        except Exception as exc:  # noqa: BLE001
            logger.debug("[tool-registry] 资源层窗口对拍失败: {}", str(exc)[:120])
        # 统一资源能力层（Phase 5）：模型可见面收敛后的差异（**哪些名字会消失**）。
        # 这是"唯一会拿走东西"的一步，因此必须先把可见面差异摊开给人看。
        try:
            from app.agents.capabilities.views.resource_surface import surface_diff

            surface_rows = surface_diff(entries_by_name().values())
            if surface_rows:
                diffs[SHADOW_MODEL_SURFACE_DIMENSION] = surface_rows
        except Exception as exc:  # noqa: BLE001
            logger.debug("[tool-registry] 模型可见面对拍失败: {}", str(exc)[:120])
    except Exception as exc:  # noqa: BLE001
        logger.debug("[tool-registry] 影子对比（意图→窗口）失败: {}", str(exc)[:120])

    # 维度 4：工具 → 审批档位（**安全边界**，必须逐条对拍）
    try:
        from app.agents.skills.approval_policy import static_tier_of

        rows = []
        declared_rows: list[list[str]] = []
        derived_entries = entries_by_name()
        for tool in sorted(derived_entries):
            declared = declared_tier_of(tool)
            if declared:
                # 显式声明的工具单独成列：静态词表**根本无法表达声明**，这种差异是
                # 有意的（"插件声明一次就生效"），不该被当成回归。它们由
                # ``tool→risk_tier(declared)`` 单独披露，不参与 switch_safe 判定。
                #
                # 行**必须与其它披露维度同为三格**（``[名字, 静态值, 派生值]``）：
                # 前端按统一形状渲染（第三格配"声明档位"这个列名）。多塞一格会让
                # 前端把"生效档位"当成"声明档位"显示——实测就是这样对不上的。
                declared_rows.append([tool, static_tier_of(tool, {})[0], declared])
                continue
            derived_tier = risk_tier_of(tool, {})
            if derived_tier is None:
                continue  # 遗留工具：注册表明确不派生，由静态词表负责
            # 基线必须是**纯静态词表**（`static_tier_of`），不能用已经混入派生的
            # `classify_tool_risk` —— 那样对拍会永远显示一致，等于没对拍。
            static_tier = static_tier_of(tool, {})[0]
            if static_tier != derived_tier:
                rows.append([tool, str(static_tier), str(derived_tier)])
        diffs["tool→risk_tier"] = rows
        if declared_rows:
            diffs[SHADOW_DECLARED_DIMENSION] = declared_rows
    except Exception as exc:  # noqa: BLE001
        logger.debug("[tool-registry] 影子对比（工具→档位）失败: {}", str(exc)[:120])

    totals = shadow_parity_totals(diffs)
    if totals["parity_total"] or totals["declared_total"]:
        logger.info(
            "[tool-registry][shadow] 静态与派生存在差异: {}",
            {k: len(v) for k, v in diffs.items()},
        )
    return diffs


def log_shadow_differences(*, scene: str = "", job_id: str = "") -> dict[str, list[list[str]]]:
    """打点"静态 vs 派生"的差异（开关 ``TOOL_REGISTRY_SHADOW_LOG``，默认开）。

    放在任务提交路径上：一次任务一次对比，成本可忽略，但能持续回答"注册表派生有没有
    跟静态表漂移"。**只记录、不改行为**——差异出现时真正该做的是修派生逻辑。

    声明带来的差异（``tool→risk_tier(declared)``）按 INFO 记录：它是**预期**的，
    不该和"派生算错了"共用同一级别的告警。
    """
    try:
        from app.core.config import settings

        if not bool(getattr(settings, "TOOL_REGISTRY_SHADOW_LOG", True)):
            return {}
    except Exception:  # noqa: BLE001
        return {}
    try:
        diffs = shadow_compare()
    except Exception as exc:  # noqa: BLE001 - 影子对比绝不能影响提交
        logger.debug("[tool-registry][shadow] 对比失败: {}", str(exc)[:120])
        return {}
    totals = shadow_parity_totals(diffs)
    if totals["missing_dimensions"]:
        logger.warning(
            "[tool-registry][shadow] 维度未算成，**不能**视为一致 scene={} job={} missing={}",
            scene or "-",
            str(job_id)[:12] or "-",
            totals["missing_dimensions"],
        )
    if totals["parity_total"]:
        logger.warning(
            "[tool-registry][shadow] 静态与派生存在差异 scene={} job={} detail={}",
            scene or "-",
            str(job_id)[:12] or "-",
            {
                key: value[:3]
                for key, value in diffs.items()
                if value and key in SHADOW_PARITY_DIMENSIONS
            },
        )
    else:
        logger.debug("[tool-registry][shadow] 静态与派生一致 scene={} job={}", scene or "-", str(job_id)[:12] or "-")
    if totals["declared_total"]:
        logger.info(
            "[tool-registry][shadow] 声明档位生效（有意差异，不计入 switch_safe）scene={} count={}",
            scene or "-",
            totals["declared_total"],
        )
    return diffs
