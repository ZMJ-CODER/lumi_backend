"""通用工作区操作工作流：读取 → 暂存修改 → 验证 → diff → 按授权提交。

区别于 workspace_code_change：不假设“代码项目 + 必须运行测试”，可服务文档/
数据/编码/普通文件整理/用户自定义 Skill。具体 SOP 由 Prompt-as-Code
（plugins/workflows/prompts/workspace_operation.md）描述，这里只提供执行壳与
安全护栏。真实审批由 ApprovalPolicyEngine 决定，Skill 不自行绕过。
"""

from __future__ import annotations

import json

from app.agents.skills.base import SkillContext, ToolOutput, WorkflowSkill
from app.platform.model.llm import LLMClient
from app.services.usage import CATEGORY_SKILL

# 与 workspace_code_change 保持一致的前缀约定：正式桌面 MCP 能力名
# mcp__{server}__{tool}；多设备部署时按工作区注册的 server 路由。
# 读取阶段只声明聚合入口；目录/搜索/单文件读取都由它的 action 完成。
#
# 写阶段分两条路（客户端两套都还在广告，见 CAPABILITY_BRIDGE.md §2/§11.5）：
#   首选 —— 原子操作四件套（write/edit/move/delete）：自带版本校验、审批、
#          回收站与读回校验，一次调用即生效，不需要 diff/commit 收尾；
#   遗留 —— 暂存对（stage_write/stage_delete）+ diff + commit + rollback，
#          仅在客户端没有原子工具时使用。
_TOOLS = (
    "mcp__lumi_client__workspace_navigator",
    "mcp__lumi_client__workspace_write",
    "mcp__lumi_client__workspace_edit",
    "mcp__lumi_client__workspace_move",
    "mcp__lumi_client__workspace_delete",
    "mcp__lumi_client__workspace_stage_write",
    "mcp__lumi_client__workspace_stage_delete",
    "mcp__lumi_client__workspace_diff",
    "mcp__lumi_client__sandbox_prepare",
    "mcp__lumi_client__sandbox_run",
    "mcp__lumi_client__sandbox_output_read",
    "mcp__lumi_client__sandbox_reset",
    "mcp__lumi_client__workspace_commit",
    "mcp__lumi_client__workspace_rollback",
)

#: 统一资源能力声明（Phase 4）：与 workspace_code_change 同一套（读/写/编辑/移动/删除 +
#: 沙箱执行），底层名字只是兼容层。见 ``app/agents/capabilities/policy/resource_workflow.py``。
_CAPABILITIES = (
    "resource.read",
    "resource.write",
    "resource.edit",
    "resource.move",
    "resource.delete",
    "code.execute",
)
_RESOURCE_TYPES = ("workspace",)
_PROVIDERS = ("workspace_provider",)

# 原子写工具按可选依赖登记：老客户端只广告暂存对时工作流仍可用，
# 不会因为新增工具把整个 Skill 判成不可用。
_OPTIONAL_TOOLS = frozenset({
    "mcp__lumi_client__workspace_write",
    "mcp__lumi_client__workspace_edit",
    "mcp__lumi_client__workspace_move",
    "mcp__lumi_client__workspace_delete",
    "mcp__lumi_client__workspace_stat",
    "mcp__lumi_client__workspace_catalog",
    "mcp__lumi_client__sandbox_prepare",
    "mcp__lumi_client__sandbox_output_read",
    "mcp__lumi_client__sandbox_reset",
    "mcp__lumi_client__workspace_rollback",
})


def _select_capabilities(capabilities):
    """选本轮可用的工作区工具：**能力声明优先，旧名字白名单兜底**（Phase 4）。"""
    try:
        from app.agents.capabilities.policy.resource_workflow import (
            select_capabilities,
            workflow_enabled,
        )

        if workflow_enabled():
            selected = select_capabilities(
                capabilities,
                capabilities=_CAPABILITIES,
                resource_types=_RESOURCE_TYPES,
                legacy_names=_TOOLS,
            )
            return selected or [item for item in capabilities if item.name in _TOOLS]
    except Exception:  # noqa: BLE001 - 选择失败不能影响工作流可用性
        pass
    return [item for item in capabilities if item.name in _TOOLS]


def _surface(capabilities):
    """收敛后的工具面：``([(capability, 对外名)], {对外名: 实现名})``（Phase 5）。"""
    try:
        from app.agents.capabilities.views.resource_surface import collapse_with_names

        return collapse_with_names(capabilities)
    except Exception:  # noqa: BLE001 - 收敛失败用原名
        return [(item, str(getattr(item, "name", "") or "")) for item in capabilities], {}


def _tool_defs(capabilities):
    definitions = []
    for capability in capabilities:
        definition = capability.to_tool_definition()
        function = definition.get("function") or {}
        schema = dict(function.get("parameters") or {})
        properties = dict(schema.get("properties") or {})
        # workspace_id 由 SkillContext 注入，模型不得编造安全敏感标识。
        properties.pop("workspace_id", None)
        required = [item for item in (schema.get("required") or []) if item != "workspace_id"]
        schema["properties"] = properties
        schema["required"] = required
        function["parameters"] = schema
        definition["function"] = function
        definitions.append(definition)
    return definitions


class WorkspaceOperationSkill(WorkflowSkill):
    name = "workspace_operation"
    description = "在用户选定的工作区中读取、暂存修改、验证并按其授权提交（覆盖文档/数据/编码/文件整理）。"
    category = "workspace"
    environment = "client"
    scenes = ["office"]
    provided_goals = ["RETRIEVE", "ANALYZE", "EXECUTE"]
    provided_sources = ["USER_INPUT", "SYSTEM_STATE", "ATTACHED_FILE"]
    safety_level = "RISKY_WRITE"
    write_op = True
    execution_scope = "backend_orchestrates_client"
    availability_policy = "require_online_client"
    fallback_policy = "clarify"
    # Skill 只声明策略与工具边界；是否需要确认由 ApprovalPolicyEngine 决定。
    approval_policy = "none"
    allowed_tools = list(_TOOLS)
    # 能力声明（Phase 4）：与 allowed_tools 并存，依赖检查只补不替。
    required_capabilities = list(_CAPABILITIES)
    resource_types = list(_RESOURCE_TYPES)
    providers = list(_PROVIDERS)
    dependencies = {
        "providers": ["desktop_mcp"],
        "tools": [{
            "name": name,
            "min_version": "1.0.0",
            "required": name not in _OPTIONAL_TOOLS,
            "provider": "desktop_mcp",
        } for name in _TOOLS],
    }
    parameters_schema = {
        "type": "object",
        "properties": {
            "instruction": {"type": "string", "description": "用户对工作区任务的目标与背景"},
        },
        "required": ["instruction"],
    }

    async def run(self, params: dict, context: SkillContext, invoke_tool) -> ToolOutput:
        if not context.user_id:
            return ToolOutput(success=False, error="需要登录后使用", error_code="INVALID_ARGS")
        if not context.workspace_id:
            return ToolOutput(success=False, error="请先选择一个工作区项目", error_code="WORKSPACE_SCOPE_REQUIRED")
        instruction = str(params.get("instruction") or params.get("question") or "").strip()
        if not instruction:
            return ToolOutput(success=False, error="缺少工作区任务目标", error_code="INVALID_PARAMS")

        from app.agents.skills.executor import get_desktop_mcp_capabilities

        capabilities = await get_desktop_mcp_capabilities(context.user_id, context.scene, "user")
        selected = _select_capabilities(capabilities)
        if not selected:
            return ToolOutput(success=False, error="当前桌面客户端未提供工作区工具", error_code="CLIENT_OFFLINE", retryable=True)

        system = context.skill_prompt or (
            "你是受控的工作区执行 Agent。每次只调用一个工具，先了解工作区再修改；"
            "修改必须通过暂存工具，任何真实提交前必须查看 workspace_diff，"
            "并由系统授权策略决定是否需要确认。"
        )
        try:
            from app.agents.capabilities.views.resource_surface import translate_prompt_names

            system = translate_prompt_names(system)
        except Exception:  # noqa: BLE001 - 翻译失败用原文
            pass
        # 统一资源能力层（Phase 5）：模型可见面收敛（关闭时对外名 = 实现名、映射为空）。
        surface_pairs, surface_alias = _surface(selected)
        visible = [
            capability if display == capability.name else capability.model_copy(update={"name": display})
            for capability, display in surface_pairs
        ]
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": instruction},
        ]
        llm = LLMClient()
        records: list[dict] = []
        diff_seen = False
        any_failed = False
        max_rounds = int(params.get("max_rounds") or 12)
        try:
            for _ in range(max_rounds):
                definitions = _tool_defs(visible)
                content, calls = await llm.chat_with_tools(
                    messages, definitions, scene=context.scene,
                    api_key=context.llm_api_key, llm_config=context.llm_config,
                    usage_user_id=context.user_id, usage_category=CATEGORY_SKILL,
                )
                if not calls:
                    return ToolOutput(success=True, output=content or "已完成工作区任务。", metadata={"steps": records})
                call = calls[0]
                function = call.get("function") or {}
                name = str(function.get("name") or "")
                args = function.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args or "{}")
                    except (TypeError, ValueError):
                        args = {}
                # 前置判断按**实现名**（模型叫 `Run` 时实现是 `sandbox_run`）。
                resolved = surface_alias.get(name, name)
                if resolved.endswith("workspace_commit") and not (diff_seen and not any_failed):
                    return ToolOutput(
                        success=False, error="提交前必须先查看工作区差异，且本任务不得存在失败工具",
                        error_code="COMMIT_GUARD_FAILED",
                    )
                result = await invoke_tool(name, args if isinstance(args, dict) else {})
                records.append({"tool": name, "status": result.status, "error_code": result.error_code})
                if not result.success:
                    any_failed = True
                if resolved.endswith("workspace_diff") and result.success:
                    diff_seen = True
                if result.status == "pending_approval":
                    return result
                assistant_message = {"role": "assistant", "content": content or None, "tool_calls": [call]}
                if call.get("reasoning_content") is not None:
                    assistant_message["reasoning_content"] = call.get("reasoning_content")
                messages.append(assistant_message)
                messages.append({
                    "role": "tool",
                    "tool_call_id": str(call.get("id") or ""),
                    "content": json.dumps(result.to_execution_envelope(), ensure_ascii=False),
                })
                if result.status in {"failed", "cancelled", "uncertain"}:
                    continue
            messages.append({"role": "user", "content": "已达到执行步数上限，请基于现有结果总结已完成内容、未完成原因和下一步建议。"})
            final = await llm.chat(messages, scene=context.scene, api_key=context.llm_api_key,
                                   llm_config=context.llm_config, usage_user_id=context.user_id,
                                   usage_category=CATEGORY_SKILL, max_tokens=3000)
            return ToolOutput(success=True, output=final, metadata={"steps": records, "max_rounds_reached": True})
        except Exception as exc:  # noqa: BLE001
            return ToolOutput(success=False, error=f"工作区执行失败：{exc}", error_code="EXEC_ERROR",
                              retryable=True, metadata={"steps": records})
