"""Workspace code change workflow: rolling plan over the formal desktop MCP."""

from __future__ import annotations

import json

from app.agents.skills.base import SkillContext, ToolOutput, WorkflowSkill
from app.core.llm import LLMClient
from app.services.usage import CATEGORY_SKILL


# 读取阶段只给聚合入口 workspace_navigator（action=list/search/read）；内部原子
# 读取名保留在 MCP 注册表与后端内部依赖中，不再进入模型 function calling schema。
# 写阶段优先原子操作四件套（CAPABILITY_BRIDGE.md §11.5），暂存对 + diff + commit
# 保留给没有原子工具的老客户端。
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

# 原子写工具与沙箱辅助按可选依赖登记：老客户端只广告暂存对时工作流仍可用。
_OPTIONAL_TOOLS = frozenset({
    "mcp__lumi_client__workspace_write",
    "mcp__lumi_client__workspace_edit",
    "mcp__lumi_client__workspace_move",
    "mcp__lumi_client__workspace_delete",
    "mcp__lumi_client__sandbox_output_read",
    "mcp__lumi_client__sandbox_reset",
    "mcp__lumi_client__workspace_rollback",
})

#: 统一资源能力声明（方案《资源能力层》Phase 4）：上面那 14 个底层名字只是**兼容层**，
#: 真正表达意图的是"对 workspace 做这几件事"。底层工具名由 Provider Adapter 解析，
#: 因此客户端换实现/新增 Provider 时不必回来改这个文件。
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


def _select_capabilities(capabilities):
    """选本轮可用的工作区工具：**能力声明优先，旧名字白名单兜底**（Phase 4）。

    开关关闭时逐字走旧的名字白名单（``item.name in _TOOLS``），行为与改造前一致。
    """
    try:
        from app.agents.capabilities.resource_workflow import (
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
            # 桌面端广告的名字与后端统一层绑定不一致时（新客户端/新 Provider），
            # 名字白名单仍然兜底——迁移期只补不替。
            return selected or [item for item in capabilities if item.name in _TOOLS]
    except Exception:  # noqa: BLE001 - 选择失败不能影响工作流可用性
        pass
    return [item for item in capabilities if item.name in _TOOLS]


def _surface(capabilities):
    """收敛后的工具面：``([(capability, 对外名)], {对外名: 实现名})``（Phase 5）。

    关闭开关时对外名 = 实现名、映射为空，逐字等于改造前。
    """
    try:
        from app.agents.capabilities.resource_surface import collapse_with_names

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
        # workspace_id is injected from SkillContext by the executor. Hiding it
        # avoids asking the model to invent a security-sensitive identifier.
        properties.pop("workspace_id", None)
        required = [item for item in (schema.get("required") or []) if item != "workspace_id"]
        schema["properties"] = properties
        schema["required"] = required
        function["parameters"] = schema
        definition["function"] = function
        definitions.append(definition)
    return definitions


class WorkspaceCodeChangeSkill(WorkflowSkill):
    name = "workspace_code_change"
    description = "在用户已选择的工作区中读取、修改并在隔离沙箱运行测试；只暂存变更，提交前等待用户审批。"
    category = "devtools"
    environment = "client"
    scenes = ["office"]
    # 编码任务的第一步经常被抽象 Planner 识别为“读取当前项目状态”
    # （RETRIEVE + SYSTEM_STATE）。如果这里只声明 EXECUTE，空项目会在
    # 读取阶段被错误降级成“缺少 Skill”，根本到不了写入流程。
    provided_goals = ["RETRIEVE", "ANALYZE", "EXECUTE"]
    # The request itself is a first-class source.  SYSTEM_STATE describes the
    # authorized workspace/sandbox; ATTACHED_FILE keeps the contract usable
    # for non-code project work that starts from uploaded material.
    provided_sources = ["USER_INPUT", "SYSTEM_STATE", "ATTACHED_FILE"]
    safety_level = "RISKY_WRITE"
    write_op = True
    execution_scope = "backend_orchestrates_client"
    availability_policy = "require_online_client"
    # 工具真正不可用时仍需澄清；“空目录”由工作流视为已获取的安全状态，
    # 不会触发此降级策略。
    fallback_policy = "clarify"
    approval_policy = "before_submit"
    allowed_tools = list(_TOOLS)
    # 能力声明（Phase 4）：迁移期与 allowed_tools **并存**（依赖检查只补不替）。
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
            "instruction": {"type": "string", "description": "用户对工作区代码任务的目标与背景"},
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
            return ToolOutput(success=False, error="缺少代码任务目标", error_code="INVALID_PARAMS")

        from app.agents.skills.executor import get_desktop_mcp_capabilities

        capabilities = await get_desktop_mcp_capabilities(context.user_id, context.scene, "user")
        selected = _select_capabilities(capabilities)
        if not selected:
            return ToolOutput(success=False, error="当前桌面客户端未提供工作区工具", error_code="CLIENT_OFFLINE", retryable=True)

        system = (context.skill_prompt or "你是一个谨慎的代码工作区执行 Agent。") + (
            "\n你负责根据目标滚动执行，不要一次性假设完整计划。每轮最多调用一个工具。"
            "先探索并读取，再修改；修改已有文件前必须先读取。"
            "改动优先用原子工具落地：新建/整体覆盖用 workspace_write，定点替换用 workspace_edit，"
            "改名/移动用 workspace_move，删除用 workspace_delete；覆盖或替换已有文件必须带本轮读取得到的 "
            "expected_revision，workspace_edit 的 old_string 必须唯一命中。原子工具自带版本校验与审批，"
            "一次调用即生效，不需要再走 diff/commit。只有客户端没有广告原子工具时，才退回暂存路径"
            "（workspace_stage_write / workspace_stage_delete → workspace_diff → workspace_commit）。"
            "无论走哪条路，都要在改动后准备沙箱并运行合适测试。测试失败时分析输出、继续读取或修复并重测。"
            "workspace_navigator(action=\"list\") 返回空列表或明确说明目录为空时，这表示工作区已成功读取且没有现有文件，"
            "不是错误、不是缺少能力，也不需要停止；在不覆盖任何现有文件的前提下继续创建用户要求的新文件。"
            "只有在工具明确返回失败、权限错误或工作区未注册时才停止并报告原因。"
            "只有走暂存路径时才需要：测试成功且查看过 workspace_diff 后，才可以调用 workspace_commit；"
            "commit 不带 approved 或 approved=false 时表示等待用户审批。不要执行删除、提交或回滚之外的隐藏动作。"
            "workspace_id 由系统注入，不要要求用户提供。结束时说清楚每个文件是已生效还是仍在暂存区待提交。"
        )
        # SOP 里的工具名做**运行期翻译**（收敛关闭时逐字不变）：业务文本仍由
        # `plugins/workflows/prompts/*.md` 维护，架构迁移不改它的内容。
        try:
            from app.agents.capabilities.resource_surface import translate_prompt_names

            system = translate_prompt_names(system)
        except Exception:  # noqa: BLE001 - 翻译失败用原文
            pass
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": instruction},
        ]
        llm = LLMClient()
        records: list[dict] = []
        tested = False
        diff_seen = False
        max_rounds = 16
        # 统一资源能力层（Phase 5）：模型可见面收敛（关闭时对外名 = 实现名、映射为空）。
        surface_pairs, surface_alias = _surface(selected)
        visible = [
            capability if display == capability.name else capability.model_copy(update={"name": display})
            for capability, display in surface_pairs
        ]
        try:
            for _ in range(max_rounds):
                definitions = _tool_defs(visible)
                content, calls = await llm.chat_with_tools(
                    messages,
                    definitions,
                    scene=context.scene,
                    api_key=context.llm_api_key,
                    llm_config=context.llm_config,
                    usage_user_id=context.user_id,
                    usage_category=CATEGORY_SKILL,
                )
                if not calls:
                    return ToolOutput(success=True, output=content or "已完成工作区代码任务。", metadata={"steps": records, "tested": tested, "diff_seen": diff_seen})
                call = calls[0]
                function = call.get("function") or {}
                name = str(function.get("name") or "")
                args = function.get("arguments") or {}
                if isinstance(args, str):
                    args = json.loads(args or "{}")
                # 前置判断按**实现名**：模型叫 `Run` 时它对应的实现是 `sandbox_run`。
                resolved = surface_alias.get(name, name)
                if resolved.endswith("workspace_commit") and not (tested and diff_seen):
                    return ToolOutput(success=False, error="提交前必须先成功运行沙箱测试并查看工作区差异", error_code="COMMIT_GUARD_FAILED")
                result = await invoke_tool(name, args if isinstance(args, dict) else {})
                records.append({"tool": name, "status": result.status, "error_code": result.error_code})
                if resolved.endswith("sandbox_run") and result.success:
                    tested = True
                if resolved.endswith("workspace_diff") and result.success:
                    diff_seen = True
                assistant_message = {"role": "assistant", "content": content or None, "tool_calls": [call]}
                if call.get("reasoning_content") is not None:
                    assistant_message["reasoning_content"] = call.get("reasoning_content")
                messages.append(assistant_message)
                messages.append({
                    "role": "tool",
                    "tool_call_id": str(call.get("id") or ""),
                    "content": json.dumps(result.to_execution_envelope(), ensure_ascii=False),
                })
                if result.status == "pending_approval":
                    return result
                if result.status in {"failed", "cancelled", "uncertain"}:
                    # Feed the complete structured error back to the model so
                    # the next turn can repair or choose a safer action.
                    continue
            messages.append({"role": "user", "content": "已达到滚动执行步数上限，请基于现有结果总结已完成内容、未完成原因和下一步建议。"})
            final = await llm.chat(messages, scene=context.scene, api_key=context.llm_api_key, llm_config=context.llm_config, usage_user_id=context.user_id, usage_category=CATEGORY_SKILL, max_tokens=3000)
            return ToolOutput(success=True, output=final, metadata={"steps": records, "tested": tested, "diff_seen": diff_seen, "max_rounds_reached": True})
        except Exception as exc:  # noqa: BLE001
            return ToolOutput(success=False, error=f"工作区代码执行失败：{exc}", error_code="EXEC_ERROR", retryable=True, metadata={"steps": records})
