"""技能插件（devtools/开发工具链）：install_new_dependencies —— 安装新增依赖（需用户确认）."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request

# 依赖安装可能较久（npm/pnpm/go），放宽客户端等待超时
INSTALL_TIMEOUT_SECONDS = 420


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class InstallNewDependenciesSkill(Skill):
    name = "install_new_dependencies"
    description = (
        "在用户主机上安装本次任务新增的依赖（npm/pnpm/yarn/pip/go 按项目自动选择）。"
        "会修改项目依赖清单与 node_modules，默认需要用户确认；"
        "安装失败时自动回滚依赖清单。"
    )
    category = "devtools"
    environment = "client"
    requires_confirmation = True  # 供应链风险：默认弹窗确认（"帮我确认"开启时自动执行）
    write_op = True
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "本地项目 ID"},
            "deps": {
                "type": "array",
                "items": {"type": "object"},
                "description": "新增依赖列表（来自 check_new_dependencies）",
            },
        },
        "required": ["project_id", "deps"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        project_id = str(params.get("project_id") or "")
        deps = params.get("deps") or []
        if not project_id or not isinstance(deps, list) or not deps:
            return SkillResult(
                success=False,
                error="缺少 project_id 或 deps",
                error_code="INVALID_ARGS",
                retryable=False,
            )
        names = "、".join(
            f"{d.get('name') or ''}@{d.get('version') or ''}" for d in deps[:10]
        )
        _notify(context, f"（正在安装新增依赖：{names}）")
        return await run_client_skill_request(
            context.user_id,
            self.name,
            {"project_id": project_id, "deps": deps},
            True,
            timeout=INSTALL_TIMEOUT_SECONDS,
            ttl=INSTALL_TIMEOUT_SECONDS + 60,
        )
