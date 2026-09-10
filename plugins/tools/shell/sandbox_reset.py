"""客户端沙箱事务：丢弃暂存修改并删除沙箱副本。"""

from app.agents.skills.base import Tool, SkillContext, ToolOutput
from app.agents.skills.executor import run_client_skill_request


class SandboxResetSkill(Tool):
    name = "sandbox_reset"
    description = (
        "重置项目沙箱，删除沙箱副本并丢弃全部暂存修改；真实项目文件不受影响。"
        "测试失败、取消或超时后使用。"
    )
    category = "shell"
    domain = "system"
    resource = "project"
    environment = "client"
    requires_confirmation = False
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {"project_id": {"type": "string", "description": "本地项目 ID"}},
        "required": ["project_id"],
    }
    use_when = ["测试或构建失败后需要丢弃本次暂存修改", "用户取消当前项目修改"]
    do_not_use_when = ["需要把修改写入真实项目时", "未提供 project_id 时"]
    result_contract = "返回已丢弃的暂存条目数量；真实项目不变。"

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        if not context or not context.user_id:
            return ToolOutput(success=False, error="需要登录后使用", error_code="AUTH_REQUIRED", retryable=False)
        project_id = str(params.get("project_id") or "").strip()
        if not project_id:
            return ToolOutput(success=False, error="缺少 project_id", error_code="INVALID_ARGS", retryable=False)
        return await run_client_skill_request(
            context.user_id, self.name, {"project_id": project_id}, False
        )

