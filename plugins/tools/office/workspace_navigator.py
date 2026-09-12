"""内部工作区导航工具：把 ``workspace_navigator`` 暴露给受控的原子步骤。

为什么需要这个内部 Tool（而不是让模型直接看到）：

* 模型可见的 ``workspace_navigator`` 由 ``execute_tool_call`` 直接分发到
  ``WorkspaceNavigatorService``（见 executor 中的 ``tool_name ==
  "workspace_navigator"`` 分支）。那条路径服务 ReAct 工具窗与工作流 Skill。
* DAG 的 ``atomic_step`` 只能执行 ``ToolRegistry`` 里已注册的工具，并且
  ``plan_compiler`` 的能力快照也只认注册过的工具 —— 所以"工作区读取快路径"
  （atomic_step(workspace_navigator) → direct_llm）必须有一个已注册的内部实现，
  否则计划编译会直接判 ``TOOL_UNAVAILABLE``。

因此本工具**以 ``public=False`` 注册**：它进入 ``_skill_implementations``，
只能被 ``get_skills_for_scene(include_internal=True)`` / ``ToolRegistry.get()``
看到（即 ``atomic_step``、能力快照与显式内部依赖），**不会**出现在
``ToolRegistry.list()``、能力发现或模型的 function calling schema 里。
模型侧的聚合入口 schema 仍然由 ``executor.get_workspace_navigator_capability``
合成，两者共用一个服务实现，不会出现两套语义。
"""

from __future__ import annotations

from app.agents.skills.base import SkillContext, Tool
from app.agents.skills.output_contract import ToolOutput
from app.services.workspace_navigator import (
    ACTIONS,
    ACTION_LIST,
    WorkspaceNavigatorService,
    model_text,
)


class WorkspaceNavigatorTool(Tool):
    """读取阶段唯一的内部原子入口：action=list/search/read/scan。"""

    name = "workspace_navigator"
    description = (
        "浏览、搜索、扫描并读取当前已授权工作区的本地资料。"
        "action=list 列出目录条目（不读正文）；action=search 按关键词定位文件；"
        "action=scan 提取代码骨架（类/函数/方法/导入 + 行号区间，不含函数体）；"
        "action=read 读取单个文件正文（目录请用 list），可用 start_line/end_line 精读某一段。"
    )
    version = "1.0.0"
    status = "stable"
    category = "workspace"
    # 执行落在后端进程内：它自己经 MCP 网关路由到托管该工作区的桌面设备，
    # 而不是被当成一个 Electron 侧同名工具去调用。
    environment = "server"
    permission = "user"
    scenes = ["office"]
    domain = "workspace"
    # 只读：不触碰真实文件，审批策略里属于 A 档。
    write_op = False
    requires_confirmation = False
    idempotent = True
    user_workflow_allowed = False
    intent_tags = ["工作区", "读取", "文件", "目录", "搜索", "资料", "代码", "结构", "骨架"]
    use_when = [
        "需要查看工作区目录结构、定位文件或读取某个文件正文",
        "需要先摸清代码文件骨架（类/函数/导入与行号）再精确精读某一段",
    ]
    do_not_use_when = ["没有绑定工作区时；需要写入/提交时改用对应的写入与提交能力"]
    result_contract = "返回统一信封：status/action/summary/data/has_more/cursor/meta/error。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS), "description": "要执行的动作"},
            "path": {
                "type": "string",
                "description": "工作区内相对路径；read/scan 必填且必须是单个文件",
            },
            "query": {"type": "string", "description": "search 的关键词或短句（OR 匹配，非正则）"},
            "search_path": {"type": "string", "description": "search 的限定目录"},
            "search_mode": {"type": "string", "enum": ["auto", "filename", "content"]},
            "depth": {"type": "integer", "description": "list 的递归深度（1-4）"},
            "cursor": {"type": "string", "description": "同一次动作的后续分页游标"},
            "start_line": {
                "type": "integer",
                "description": (
                    "scan 的扫描起始行；read 的读取起始行（配合 end_line 精读某一段，"
                    "而不是从头顺序读）"
                ),
            },
            "end_line": {"type": "integer", "description": "scan/read 的结束行（含端点）"},
            "kind": {
                "type": "string",
                "enum": ["class", "function", "method", "import"],
                "description": "scan 只返回该类型的符号",
            },
            "find": {"type": "string", "description": "scan 时按名字定位单个符号（给出它的行区间）"},
            "max_symbols": {"type": "integer", "description": "scan 返回的符号上限（1-400）"},
            "include_imports": {"type": "boolean", "description": "scan 是否返回导入依赖列表"},
            "max_chars": {
                "type": "integer",
                "description": "read 每页返回的字符数（分页粒度，不是文件可读总量）",
            },
            "read_full": {
                "type": "boolean",
                "description": (
                    "read 是否按页连续读取直到文件结束（默认 true）。"
                    "达到单次页数上限仍未读完时会返回 has_more=true 与 cursor，"
                    "继续读取直到 has_more 为 false 才算读完整份。"
                ),
            },
            "read_to_end": {
                "type": "boolean",
                "description": (
                    "内部完整读取标记：仅在用户明确要求全文/整份/通读时使用。"
                    "系统会沿 cursor 继续到文件结尾；达到安全预算仍返回 has_more。"
                ),
            },
            "max_results": {"type": "integer", "description": "list/search 本页最大条数"},
            "include_ignored": {"type": "boolean", "description": "list 是否返回被忽略条目"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }
    # 工作区由服务端注入：Planner 必须显式给出 action，path 可以留空
    # （例如 list 根目录），read 的 path 由 _validate_explicit_inputs 保证。
    plan_required_fields = ["action"]
    direct_instruction_field = "query"

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        if context is None or not context.user_id:
            return ToolOutput(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        workspace_id = str(context.workspace_id or "").strip()
        if not workspace_id:
            return ToolOutput(
                success=False,
                error="当前任务没有已选择的工作区；请先在办公模式中新建或打开项目",
                error_code="WORKSPACE_SCOPE_REQUIRED",
                retryable=False,
            )
        args = dict(params or {})
        args.pop("workspace_id", None)
        action = str(args.get("action") or ACTION_LIST).strip().casefold()
        if action not in ACTIONS:
            allowed = "/".join(str(item) for item in ACTIONS)
            return ToolOutput(
                success=False,
                error=f"action 只能是 {allowed}，收到：{action or '（空）'}",
                error_code="INVALID_ACTION",
                retryable=False,
            )
        service = WorkspaceNavigatorService(
            user_id=context.user_id,
            user_role="user",
            workspace_id=workspace_id,
            conversation_id=str(context.conversation_id or context.job_id or ""),
            device_id="",
            request=str(context.skill_prompt or ""),
        )
        payload = await service.execute(action, args)
        status = str(payload.get("status") or "error")
        ok = status in {"ok", "partial", "empty"}
        counts = 0
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        # symbols 也计数：scan 的结果条数是骨架里的符号数（不含正文）。
        for key in ("entries", "matches", "sections", "symbols"):
            value = data.get(key)
            if isinstance(value, list):
                counts += len(value)
        result = ToolOutput(
            status="success" if ok else "failed",
            output=model_text(payload),
            data=payload,
            content_type="structured",
            error=None if ok else str((payload.get("error") or {}).get("message") or "工作区读取失败"),
            error_code=None if ok else str((payload.get("error") or {}).get("code") or "") or None,
            retryable=False,
            metadata={
                "tool": "workspace_navigator",
                "unified_read": True,
                "navigator_action": action,
                "result_count": counts,
                "workspace_version": (payload.get("meta") or {}).get("workspace_version"),
                "decision_signals": {
                    "result_count": counts,
                    "more_available": bool(payload.get("has_more")),
                },
            },
        )
        # 自己在产出边界做**字段级**清洗：保留工作区相对路径与解析正文，只清
        # 服务端路径形态与敏感字段。这样即使执行器随后按 environment="server"
        # 再洗一遍（同样的规则、幂等），也不会把合法正文或 path 字段洗掉。
        from app.core.agent_security import sanitize_workspace_result

        return sanitize_workspace_result(result)


__all__ = ["WorkspaceNavigatorTool"]
