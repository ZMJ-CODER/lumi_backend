"""技能插件（devtools/开发工具链）：check_new_dependencies —— 检测生成代码新增的依赖."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class CheckNewDependenciesSkill(Skill):
    name = "check_new_dependencies"
    description = (
        "检查本地项目中依赖清单（package.json / requirements.txt / go.mod 等）"
        "相对本次任务开始前是否新增了依赖，返回新增项列表。"
        "测试/构建前调用，避免新依赖未安装导致构建失败。"
    )
    category = "devtools"
    environment = "client"
    requires_confirmation = False
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "本地项目 ID"},
        },
        "required": ["project_id"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        project_id = str(params.get("project_id") or "")
        if not project_id:
            return SkillResult(success=False, error="缺少 project_id", error_code="INVALID_ARGS", retryable=False)
        _notify(context, "（正在检查新增依赖…）")
        return await run_client_skill_request(
            context.user_id,
            self.name,
            {"project_id": project_id},
            False,
        )
