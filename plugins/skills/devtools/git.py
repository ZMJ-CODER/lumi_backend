"""技能插件（devtools/开发工具链）：git —— 本地仓库状态/差异/提交."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class GitSkill(Skill):
    name = "git"
    description = (
        "在本地 git 仓库执行常用操作：status（查看改动）、diff（查看差异）、"
        "commit（提交全部改动）。提交是写操作，执行前需要用户确认。"
    )
    category = "devtools"
    environment = "client"
    requires_confirmation = False
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "diff", "commit"],
                "description": "要执行的操作",
            },
            "cwd": {"type": "string", "description": "仓库目录（绝对路径）"},
            "message": {"type": "string", "description": "commit 时的提交信息（action=commit 必填）"},
        },
        "required": ["action", "cwd"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        action = str(params.get("action") or "").strip()
        cwd = str(params.get("cwd") or "").strip()
        if action not in ("status", "diff", "commit"):
            return SkillResult(
                success=False,
                error="action 仅支持 status / diff / commit",
                error_code="INVALID_ARGS",
                retryable=False,
            )
        if not cwd:
            return SkillResult(success=False, error="缺少仓库目录 cwd", error_code="INVALID_ARGS", retryable=False)
        require_confirm = action == "commit"
        _notify(
            context,
            f"（正在执行 git {action}" + (f"：{str(params.get('message') or '')[:40]}" if action == "commit" else "") + "）",
        )
        return await run_client_skill_request(
            context.user_id,
            self.name,
            {
                "action": action,
                "cwd": cwd,
                "message": str(params.get("message") or ""),
            },
            require_confirm,
        )
