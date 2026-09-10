"""统一审批策略引擎（ApprovalPolicyEngine）。

所有执行路径（DAG 节点、Workflow Skill、ReAct、MCP Tool、Redis 兼容通道、
Temporal 恢复）都应经由本引擎决定是否需要在当前调用前征得用户确认，避免
“后端批准了、Electron 又弹一次”或“前端自动批准、后端却提前拦截”。

引擎只依据结构化输入做决定，不读任何 Prompt 文本：
  - tool：限定名（mcp__server__tool）或工具名；
  - arguments：本次调用的参数（用于路径/命令风险判定）；
  - workspace_context：{workspace_available, approval_mode, ...}；
  - execution_grant：执行授权快照（approval_mode / approved 标记等）；
  - task_context：任务级信息（暂未使用，保留给 Temporal/重放语义）。

三档语义（与产品方案一致）：
  A. 默认自动执行：只影响暂存/读取/沙箱准备，不立即破坏真实工作区；
  B. “帮我确认”开启（auto_routine）自动执行；关闭（manual_commit）只在
     最终真实写入前确认一次（scope=task）；
  C. 始终确认：即使开启也不能自动执行（回滚/根删除/强推/凭据/外部副作用等）。

Skill/Workflow 只声明 approval_policy/risk_hints/allowed_tools，不得自行决定
绕过审批；真实决策在此集中。

覆盖范围：DAG 节点、Workflow Skill、ReAct、MCP Tool、Redis 兼容通道与
Temporal 恢复执行最终都会调用统一的 executor（execute_tool_call），其中
workspace_/sandbox_ 能力调用统一经本引擎裁决，保证各路径不再各自弹确认。
非工作区遗留工具（本地 server/sandbox、通用客户端 Read/Write 等）在迁移完成
前仍沿用各自声明的 requires_confirmation（其确认弹窗本身也来自用户端），避免
一次性放大审批面；新工作区能力一律以本引擎为准。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

APPROVAL_MODE_AUTO = "auto_routine"
APPROVAL_MODE_CONFIRM = "manual_commit"
_DEFAULT_MODE = APPROVAL_MODE_CONFIRM

Decision = Literal["allow", "require_confirmation", "deny"]
Risk = Literal["low", "medium", "high", "critical"]
Scope = Literal["call", "task", "workspace"]
Tier = Literal["auto", "routine", "critical"]

# A 档：默认自动（暂存写入、读取、统一内容提取、沙箱准备/重置，不触碰真实文件）。
_TIER_AUTO_RAW = frozenset({
    "workspace_catalog", "workspace_list", "workspace_stat", "workspace_read",
    "workspace_search", "workspace_content_extract", "workspace_diff",
    "workspace_stage_write", "workspace_stage_delete",
    "sandbox_prepare", "sandbox_reset",
    "read", "glob", "grep", "filestat", "openfile", "read_document",
    "office_doc_read", "inspect_document_set", "get_project_context",
})

# B 档：普通真实写入/提交/工作区脚本运行。
_TIER_ROUTINE_RAW = frozenset({
    "workspace_commit",
    "write", "edit", "notebookedit", "office_doc_edit", "office_doc_analyze",
    "bash", "shell", "run_in_sandbox", "sandbox_run", "python_exec",
    "create_office_document", "todo_manager",
})

# C 档：始终确认。
_TIER_CRITICAL_RAW = frozenset({
    "workspace_rollback",
    "git_reset_hard", "git_clean", "git_force_push",
    "delete_workspace_root",
})

# 高风险命令/凭据特征（C 档判定用，保守命中即升级）。
_CRITICAL_CMD_MARKERS = (
    "reset --hard", "clean -f", "clean -fd", "force push", "push --force",
    "rm -rf /", "sudo ", "chmod 777 ", "chown ",
)
_SAFE_TEST_CMD_MARKERS = (
    "pytest", "python -m pytest", "go test", "make check", "flake8", "ruff",
    "mypy", "tsc --noemit", "npm test", "pnpm test", "yarn test",
    "npm run build", "pnpm run build", "yarn build", "make build",
)
_CRITICAL_PATH_MARKERS = (
    ".env", ".pem", ".key", "id_rsa", "credentials", "secrets",
    "token", "password", "aws", "gcloud", "kubeconfig",
)
_ROOT_LIKE = {"", ".", "..", "/", "*", "**", "/.", "./"}


def _base_name(tool_name: str) -> str:
    """mcp__server__tool → tool；其它原样。"""
    name = str(tool_name or "")
    if "__" in name and not name.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) == 3:
            return parts[2]
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return parts[2]
    return name.casefold()


def _path_value(arguments: dict | None) -> str:
    for key in ("path", "file_path", "target_file", "notebook_path", "cwd"):
        value = (arguments or {}).get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _is_root_like(path: str) -> bool:
    return str(path or "").strip() in _ROOT_LIKE


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """一次工具调用的审批决策结果。"""

    decision: Decision
    risk: Risk
    reason: str
    scope: Scope = "call"
    tier: Tier = "routine"


def classify_tool_risk(tool_name: str, arguments: dict | None = None) -> tuple[Tier, Risk, str]:
    """按工具名 + 参数把调用分到 A/B/C 档并给出风险等级。"""
    raw = _base_name(tool_name)
    args = dict(arguments or {})
    path = _path_value(args)
    command = str(args.get("command") or args.get("script") or "").casefold()
    recursive = bool(args.get("recursive") or args.get("force") or args.get("-r"))

    if raw in _TIER_CRITICAL_RAW:
        return "critical", "critical", f"工具 {raw} 属于始终确认操作"
    if raw == "workspace_stage_delete" and (_is_root_like(path) or (recursive and "*" in str(path or ""))):
        return "critical", "critical", "删除范围疑似工作区根目录或批量通配"
    if raw in {"workspace_read", "read"} and path and any(marker in path.casefold() for marker in _CRITICAL_PATH_MARKERS):
        # 读敏感文件不算逃逸，但导出/回显前需确认（引擎按 C 处理更安全）。
        return "critical", "high", "目标路径疑似凭据/密钥文件"
    if raw in _TIER_ROUTINE_RAW and command and any(marker in command for marker in _CRITICAL_CMD_MARKERS):
        return "critical", "critical", "命令包含高风险操作（强制重置/清理/提权/强推）"
    if raw in {"sandbox_run", "bash", "shell", "run_in_sandbox"} and command:
        # 白名单内的测试/检查/构建命令属于 A 档自动执行（不立即破坏真实工作区）。
        if any(marker in command for marker in _SAFE_TEST_CMD_MARKERS):
            return "auto", "low", "白名单内的测试/检查/构建命令"
    if raw in _TIER_AUTO_RAW:
        return "auto", "low", "只读或暂存层操作，不立即破坏真实工作区"
    if raw in _TIER_ROUTINE_RAW:
        return "routine", "medium", "普通工作区写入/提交/脚本运行"
    if raw.startswith(("workspace_", "sandbox_")):
        return "routine", "medium", f"工作区工具 {raw}，按例行策略处理"
    # 未知/非工作区工具：保守按 C 档。
    return "critical", "high", f"工具 {raw} 不在工作区策略词表内，需人工确认"


def _is_expired(value: str) -> bool:
    """ISO-8601（或数字 epoch）过期判定；解析失败视为未过期（不过度拦截）。"""
    text = str(value or "").strip()
    if not text:
        return False
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            epoch = float(text)
        except ValueError:
            return False
        return time_now() > epoch
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt < datetime.now(timezone.utc)


def time_now() -> float:
    return datetime.now(timezone.utc).timestamp()


def should_confirm(
    *,
    tool: str,
    arguments: dict | None = None,
    workspace_context: dict[str, Any] | None = None,
    execution_grant: dict[str, Any] | None = None,
    task_context: dict[str, Any] | None = None,
) -> PolicyDecision:
    """统一审批入口：返回 allow / require_confirmation / deny。

    ``execution_grant`` 内 ``approved`` 为真表示用户已在该任务中完成一次性
    确认（用于 B 档 manual_commit 的“最终写入前确认一次”语义）。
    授权快照（workspace_context/execution_grant 的 expires_at）过期后，自动
    模式降级为逐次确认模式，防止过期快照被继续当作“帮我确认已开启”。
    """
    del task_context  # 预留：Temporal/重放时可按任务级授权放行
    raw = _base_name(tool)
    grant = dict(execution_grant or {})
    workspace = dict(workspace_context or {})
    mode = str(grant.get("approval_mode") or workspace.get("approval_mode") or _DEFAULT_MODE)
    if mode == "confirm_on_write":  # 兼容早期后端快照
        mode = APPROVAL_MODE_CONFIRM
    if mode not in {APPROVAL_MODE_AUTO, APPROVAL_MODE_CONFIRM}:
        mode = _DEFAULT_MODE
    workspace_available = bool(workspace.get("workspace_available", grant.get("workspace_available", False)))
    expired = _is_expired(str(workspace.get("expires_at") or grant.get("expires_at") or ""))
    if expired and mode == APPROVAL_MODE_AUTO:
        mode = APPROVAL_MODE_CONFIRM
    expiry_note = "；授权快照已过期，已降级为逐次确认" if expired else ""

    if not workspace_available and raw.startswith(("workspace_", "sandbox_")):
        return PolicyDecision(
            "deny", "high", "工作区不可用，禁止调用工作区工具", scope="workspace", tier="critical"
        )

    tier, risk, reason = classify_tool_risk(tool, arguments)
    already_approved = bool(grant.get("approved") or workspace.get("approved"))

    if tier == "auto":
        return PolicyDecision("allow", risk, reason, scope="call", tier=tier)
    if tier == "routine":
        if mode == APPROVAL_MODE_AUTO or already_approved:
            return PolicyDecision("allow", risk, reason, scope="task", tier=tier)
        return PolicyDecision(
            "require_confirmation", risk,
            reason + "（“帮我确认”关闭：真实写入前确认一次）" + expiry_note,
            scope="task", tier=tier,
        )
    # critical：始终确认（auto_routine 也不豁免）；除非本次调用已带用户批准标记。
    if already_approved and str(grant.get("explicit_critical") or ""):
        return PolicyDecision("allow", risk, reason, scope="workspace", tier=tier)
    return PolicyDecision(
        "require_confirmation", risk,
        reason + "（高风险操作，即使开启“帮我确认”也不能自动执行）" + expiry_note,
        scope="workspace", tier=tier,
    )
