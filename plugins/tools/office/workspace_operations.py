"""内部工作区操作工具：``workspace_write`` / ``workspace_edit`` / ``workspace_move`` /
``workspace_delete`` —— 统一走 OperationResult 契约的四个原子操作。

为什么既有内部 Tool、也让模型能拿到：

* **模型可见**：写阶段（工作区、出现创建/修改/删除/移动意图时）由 ReAct 把四个工具
  注入候选窗，模型可以自由调用；读取阶段仍然只给聚合入口 ``workspace_navigator``；
* **内部可用**：DAG 的 ``atomic_step`` 只能执行 ``ToolRegistry`` 里注册过的工具，
  ``plan_compiler`` 的能力快照也只认注册过的工具，所以必须有正式 Tool 实现，
  否则计划编译直接判 ``TOOL_UNAVAILABLE``；
* **执行只有一个实现**：``app.services.workspace_operations``（版本校验 → 审批 →
  回收站 → 读回校验 → ``OperationResult``）。本文件只负责把上下文注进去并把结果折成
  既有 ``ToolOutput``，不重复任何业务逻辑。
"""

from __future__ import annotations

from typing import Any

from app.agents.skills.base import SkillContext, Tool
from app.agents.skills.output_contract import ToolOutput
from app.contracts.operations import (
    OperationApprovalState,
    OperationContext,
    OperationKind,
)
from app.services.workspace_operations import (
    NavigatorWorkspaceClient,
    WorkspaceOperationService,
)


def _context_from(skill_context: SkillContext | None, params: dict[str, Any]) -> OperationContext:
    """从 Skill 上下文构造操作上下文（身份只来自服务端/调用方注入的上下文）。"""
    metadata = getattr(skill_context, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    approval = str(metadata.get("approval_state") or "").strip()
    return OperationContext(
        request_id=str(metadata.get("request_id") or ""),
        trace_id=str(metadata.get("trace_id") or ""),
        conversation_id=str(getattr(skill_context, "conversation_id", "") or ""),
        job_id=str(getattr(skill_context, "job_id", "") or ""),
        node_id=str(getattr(skill_context, "node_id", "") or ""),
        step_id=str(metadata.get("step_id") or ""),
        user_id=str(getattr(skill_context, "user_id", "") or ""),
        workspace_id=str(getattr(skill_context, "workspace_id", "") or ""),
        device_id=str(metadata.get("device_id") or ""),
        lease_id=str(metadata.get("lease_id") or ""),
        provider_id=str(metadata.get("provider_id") or ""),
        idempotency_key=str(params.get("idempotency_key") or metadata.get("idempotency_key") or ""),
        approval_token=str(metadata.get("approval_token") or ""),
        approval_state=(
            OperationApprovalState.APPROVED
            if approval in {OperationApprovalState.APPROVED.value, "approved", "granted"}
            else OperationApprovalState.NOT_REQUIRED
        ),
        workspace_version=int(metadata.get("workspace_version") or 0),
        dry_run=bool(params.get("dry_run")),
    )


class _WorkspaceOperationTool(Tool):
    """三个操作工具的共同实现（执行 = 操作网关）。"""

    kind: OperationKind = OperationKind.EDIT
    expected_capability: str = ""

    environment = "server"
    permission = "user"
    category = "workspace"
    domain = "workspace"
    scenes = ["office"]
    write_op = True
    requires_confirmation = True
    idempotent = False
    user_workflow_allowed = False
    resource_templates = ["workspace:{workspace_id}"]

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        if context is None or not context.user_id:
            return ToolOutput(
                success=False,
                error="需要登录后使用",
                error_code="INVALID_ARGS",
                retryable=False,
            )
        workspace_id = str(context.workspace_id or "").strip()
        if not workspace_id:
            return ToolOutput(
                success=False,
                error="当前任务没有已选择的工作区；请先在办公模式中新建或打开项目",
                error_code="WORKSPACE_SCOPE_REQUIRED",
                retryable=False,
            )
        args = {key: value for key, value in dict(params or {}).items() if key != "workspace_id"}
        operation_context = _context_from(context, args)
        service = WorkspaceOperationService(
            NavigatorWorkspaceClient(
                _navigator(context, workspace_id=workspace_id, request=str(context.skill_prompt or ""))
            ),
            context=operation_context,
        )
        result = await service.execute(self.kind, args)
        output = result.to_tool_output(tool_name=self.name)
        output.metadata = {
            **output.metadata,
            "capability": self.expected_capability,
            "operation_summary": {
                "operation": str(result.kind),
                "status": str(result.status),
                "logical_path": result.logical_path,
                "target_path": result.target_path,
                "revision": result.new_revision or result.old_revision,
                "changed_files": result.affected_files,
                "approval_state": str(result.approval_state),
                "rollback_available": result.rollback_available,
            },
        }
        return output


def _navigator(context: SkillContext, *, workspace_id: str, request: str):
    from app.services.workspace_navigator import WorkspaceNavigatorService

    return WorkspaceNavigatorService(
        user_id=str(context.user_id or ""),
        user_role="user",
        workspace_id=workspace_id,
        conversation_id=str(context.conversation_id or context.job_id or ""),
        request=request,
    )


class WorkspaceWriteTool(_WorkspaceOperationTool):
    """写入/新建工作区文件（覆盖已有文件必须带 expected_revision）。"""

    name = "workspace_write"
    description = (
        "把完整内容写入工作区文件（新建或覆盖，服务端做版本校验 + 审批 + 读回校验）。"
        "覆盖已有文件必须带 expected_revision（先用 workspace_navigator action=read 拿到 revision），"
        "避免基于过期内容盲写；空内容默认拒绝，确需清空要显式 allow_empty=true。"
    )
    version = "1.0.0"
    kind = OperationKind.WRITE
    expected_capability = "workspace.write@1"
    idempotent = True
    intent_tags = ["工作区", "写入", "新建", "创建", "保存", "落盘"]
    use_when = ["需要创建新文件，或按完整内容覆盖已有文件"]
    do_not_use_when = ["只改一小段时用 workspace_edit（避免整篇重写）"]
    result_contract = "返回统一 OperationResult（status/revision/changes/approval_state/error）。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "工作区内相对路径"},
            "content": {"type": "string", "description": "完整文件内容"},
            "expected_revision": {
                "type": "string",
                "description": "覆盖已有文件时的当前版本（读取时拿到）",
            },
            "allow_empty": {"type": "boolean", "description": "显式允许写入空内容（默认拒绝）"},
            "create_parents": {"type": "boolean", "description": "自动创建父目录（默认 true）"},
            "newline": {
                "type": "string",
                "enum": ["preserve", "lf", "crlf"],
                "description": "换行风格：默认 preserve（沿用已有文件）",
            },
            "dry_run": {"type": "boolean", "description": "只预览不落盘"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }
    plan_required_fields = ["path", "content"]


class WorkspaceEditTool(_WorkspaceOperationTool):
    """按 ``old_str`` → ``new_str`` 严格匹配编辑（默认要求唯一匹配）。"""

    name = "workspace_edit"
    description = (
        "编辑工作区中的单个文本文件：读取 → 版本校验 → 严格匹配替换 → 原子提交。"
        "默认要求 old_str 唯一匹配（多处匹配会拒绝），必须带 expected_revision。"
    )
    version = "1.0.0"
    kind = OperationKind.EDIT
    expected_capability = "workspace.edit@1"
    intent_tags = ["工作区", "编辑", "修改", "替换", "代码"]
    use_when = ["需要精确替换文件里的某一段内容（而不是整篇重写）"]
    do_not_use_when = ["需要创建/整体覆盖文件时用写入；需要移动/删除时用对应工具"]
    result_contract = "返回统一 OperationResult（status/revision/changes/approval_state/error）。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "工作区内相对路径（单个文件）"},
            "old_str": {"type": "string", "description": "要替换的原文（必须唯一匹配）"},
            "new_str": {"type": "string", "description": "替换后的内容"},
            "expected_revision": {"type": "string", "description": "读取时拿到的版本（必填）"},
            "occurrence": {"type": "integer", "description": "只替换第 N 处（1-based，默认 0=必须唯一）"},
            "replace_all": {"type": "boolean", "description": "显式替换全部匹配（危险）"},
            "dry_run": {"type": "boolean", "description": "只预览不落盘"},
        },
        "required": ["path", "old_str", "new_str"],
        "additionalProperties": False,
    }
    plan_required_fields = ["path", "old_str", "new_str"]


class WorkspaceMoveTool(_WorkspaceOperationTool):
    """移动/重命名（同一文件系统内单次 rename：文件与目录都原子）。"""

    name = "workspace_move"
    description = (
        "移动或重命名工作区内的文件或目录：客户端用**同一文件系统内的单次 rename** 完成"
        "（文件与目录都原子）。跨设备移动直接拒绝（不先复制再删除）；目标已存在时拒绝"
        "（rename 不覆盖）；目录移动会校验循环。"
    )
    version = "1.0.0"
    kind = OperationKind.MOVE
    expected_capability = "workspace.move@1"
    intent_tags = ["工作区", "移动", "重命名", "整理"]
    use_when = ["需要把文件或目录搬家/改名，且希望保留历史与审批"]
    do_not_use_when = ["需要跨设备搬运（不支持）；需要覆盖已存在的目标（先删除再移动）"]
    result_contract = "返回统一 OperationResult（status/revision/changed_files/error）。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "source_path": {"type": "string", "description": "源路径（工作区相对路径）"},
            "target_path": {"type": "string", "description": "目标路径（工作区相对路径）"},
            "expected_revision": {
                "type": "string",
                "description": "源版本：文件=内容版本，目录=目录快照版本（dir1:…）",
            },
            "overwrite": {
                "type": "boolean",
                "description": "兼容字段：客户端 rename 不覆盖，目标存在时仍会拒绝",
            },
            "dry_run": {"type": "boolean", "description": "只预览不落盘"},
        },
        "required": ["source_path", "target_path"],
        "additionalProperties": False,
    }
    plan_required_fields = ["source_path", "target_path"]


class WorkspaceDeleteTool(_WorkspaceOperationTool):
    """删除到回收站（默认可恢复）；永久删除走高危审批。"""

    name = "workspace_delete"
    description = (
        "删除工作区文件或目录：默认用 rename 原子移入 .lumi_trash 回收站（可恢复，"
        "不读内容 ⇒ 目录与二进制同样支持）；非空目录必须显式 recursive；"
        "permanent=true 为不可恢复的高危删除，始终需要本机确认。"
    )
    version = "1.0.0"
    kind = OperationKind.DELETE
    expected_capability = "workspace.delete@1"
    intent_tags = ["工作区", "删除", "回收站", "清理"]
    use_when = ["需要删除文件或目录，并希望可恢复/可审计"]
    do_not_use_when = ["需要清理回收站自身（用 purge 接口）"]
    result_contract = "返回统一 OperationResult（status/stats/trash_id/rollback_available）。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "工作区内相对路径"},
            "recursive": {"type": "boolean", "description": "非空目录必须显式传 true"},
            "permanent": {"type": "boolean", "description": "永久删除（不可恢复，需审批）"},
            "expected_revision": {"type": "string", "description": "删除前版本（建议必填）"},
            "dry_run": {"type": "boolean", "description": "只预览不删除"},
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    plan_required_fields = ["path"]


__all__ = [
    "WorkspaceDeleteTool",
    "WorkspaceEditTool",
    "WorkspaceMoveTool",
    "WorkspaceWriteTool",
]
