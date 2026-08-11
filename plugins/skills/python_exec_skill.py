"""技能插件：python_exec —— 在隔离沙箱中执行 Python 代码."""

from app.agents.sandbox.registry import get_sandbox
from app.agents.skills.base import Skill, SkillContext, SkillResult


class PythonExecSkill(Skill):
    name = "python_exec"
    description = (
        "在隔离沙箱中执行一段 Python 代码。用于数学计算、数据分析、格式转换等"
        "需要真实执行的场景。代码在受限环境运行（超时/输出截断），不能访问网络。"
    )
    category = "computation"
    environment = "sandbox"
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "要执行的 Python 代码"},
            "timeout": {"type": "integer", "description": "超时秒数（默认 20，最大 60）", "minimum": 1, "maximum": 60},
        },
        "required": ["code"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        code = str(params.get("code") or "").strip()
        if not code:
            return SkillResult(
                success=False,
                error="缺少要执行的代码 code",
                error_code="INVALID_ARGS",
                retryable=False,
            )
        timeout = min(int(params.get("timeout") or 20), 60)
        sandbox = get_sandbox()
        result = await sandbox.run_script(code, language="python", timeout=timeout)
        if result.status == "success":
            return SkillResult(success=True, output=result.stdout or "(无输出)")
        if result.status == "timeout":
            return SkillResult(
                success=False,
                error=f"代码执行超时（>{timeout}s）",
                error_code="TIMEOUT",
                retryable=False,
                metadata={"stderr": result.stderr},
            )
        return SkillResult(
            success=False,
            error=result.error or result.stderr or "代码执行失败",
            error_code="EXEC_ERROR",
            retryable=False,
            metadata={"stderr": result.stderr},
        )
