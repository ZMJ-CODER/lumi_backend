"""客户端沙箱事务：经用户确认后提交暂存修改。"""

from app.agents.skills.base import Tool, SkillContext, ToolOutput
from app.agents.skills.executor import run_client_skill_request


class SandboxCommitSkill(Tool):
    name = "sandbox_commit"
    description = (
        "将项目沙箱中的暂存修改提交到真实项目文件。必须经过用户确认；"
        "未确认、失败或取消时真实项目保持不变。"
    )
    category = "shell"
    domain = "system"
    resource = "project"
    environment = "client"
    requires_confirmation = True
    write_op = True
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {"project_id": {"type": "string", "description": "本地项目 ID"}},
        "required": ["project_id"],
    }
    use_when = ["用户明确确认将已验证的沙箱修改写入项目"]
    do_not_use_when = ["尚未测试或用户未确认时", "只想撤销修改时"]
    result_contract = "返回提交文件数量；只有客户端确认后才会改变真实项目。"

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        if not context or not context.user_id:
            return ToolOutput(success=False, error="需要登录后使用", error_code="AUTH_REQUIRED", retryable=False)
        project_id = str(params.get("project_id") or "").strip()
        if not project_id:
            return ToolOutput(success=False, error="缺少 project_id", error_code="INVALID_ARGS", retryable=False)
        return await run_client_skill_request(
            context.user_id, self.name, {"project_id": project_id}, True
        )

