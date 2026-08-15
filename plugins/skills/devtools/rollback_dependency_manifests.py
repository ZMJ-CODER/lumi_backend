"""技能插件（devtools/开发工具链）：rollback_dependency_manifests —— 回滚本次任务改动的依赖清单."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class RollbackDependencyManifestsSkill(Skill):
    name = "rollback_dependency_manifests"
    description = (
        "把本次任务改动过的依赖清单（package.json / requirements.txt / go.mod 等）"
        "恢复为任务开始前的版本（有流式备份则从备份恢复，暂存修改则丢弃暂存）。"
        "测试失败或安装失败后调用，避免把用户项目搞脏。"
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
        _notify(context, "（正在回滚依赖清单…）")
        return await run_client_skill_request(
            context.user_id,
            self.name,
            {"project_id": project_id},
            False,
        )
