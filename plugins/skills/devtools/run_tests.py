"""技能插件（devtools/开发工具链）：run_tests —— 运行项目测试套件."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


async def _detect_test_command(session, user_id: str, project_id: str) -> str | None:
    """按项目文件自动选择测试命令（白名单内）."""
    from app.services import project_index

    files = await project_index.list_project_files(session, user_id, project_id, limit=500)
    lower = [f.lower() for f in files]
    if "package.json" in lower:
        return "npm test"
    if any(f.endswith(".py") for f in lower):
        return "pytest -q"
    if any(f.endswith(".go") for f in lower):
        return "go test ./..."
    if any(f.endswith(".rs") for f in lower):
        return "cargo test"
    if any(f.endswith(".java") for f in lower):
        return "mvn test"
    return None


class RunTestsSkill(Skill):
    name = "run_tests"
    description = (
        "运行项目测试套件（自动识别：npm test / pytest / go test / cargo test / mvn test 等），"
        "返回测试结果。代码修改后验证是否破坏功能时使用。"
    )
    category = "devtools"
    environment = "client"
    requires_confirmation = False
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "本地项目 ID"},
            "command": {"type": "string", "description": "可选：覆盖自动识别的测试命令"},
        },
        "required": ["project_id"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        project_id = str(params.get("project_id") or "")
        if not project_id:
            return SkillResult(success=False, error="缺少 project_id", error_code="INVALID_ARGS", retryable=False)
        command = str(params.get("command") or "").strip()
        if not command:
            try:
                from app.core.database import async_session_factory

                async with async_session_factory() as session:
                    command = await _detect_test_command(session, context.user_id, project_id)
            except Exception:  # noqa: BLE001
                command = None
        if not command:
            return SkillResult(
                success=False,
                error="无法识别项目测试命令，请用 command 显式指定（如 npm test / pytest -q）",
                error_code="EXEC_ERROR",
                retryable=False,
            )
        _notify(context, f"（正在运行测试：{command}）")
        return await run_client_skill_request(
            context.user_id,
            self.name,
            {"project_id": project_id, "command": command},
            False,
        )
