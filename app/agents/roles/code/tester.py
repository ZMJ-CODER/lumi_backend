"""测试 agent：按项目类型自动选择验证命令（构建/测试，能正常退出）并如实汇报结果."""

from app.agents.core.base import WorkerAgent, WorkerContext
from app.agents.core.tools import TEST_SYSTEM_PROMPT, list_project_files
from app.agents.orchestration.models import TaskNode
from app.agents.orchestration.progress import set_progress as _report_progress


class CodeTesterAgent(WorkerAgent):
    """测试 agent：按项目类型自动选择验证命令（构建/测试，能正常退出）并如实汇报结果.

    注意：dev 服务器不会退出，不适合自动验证；简单前端项目用 npm run build 校验即可。
    执行结果以 success=True + tests_passed 汇报，避免命令失败触发 DAG 重试导致重复确认弹窗。
    """

    name = "code_tester"
    description = "在本地项目自动选择并运行合适的测试/构建命令，如实汇报结果"
    params_help = 'params 用 {"project_id": "项目ID"}，不要预设 command，由 tester 根据项目文件自行决定'
    skills = ["read_project_file", "write_project_file", "run_project_command"]

    _TEST_PROMPT = TEST_SYSTEM_PROMPT

    async def _pick_command(self, project_id: str, files: list[str], requested: str) -> str | None:
        """按项目类型选择最合适的验证命令."""
        if requested:
            return requested
        files_l = [f.lower() for f in files]
        has_pkg = "package.json" in files_l
        has_test_infra = any(
            f.endswith((".test.ts", ".test.js", ".test.tsx", ".test.jsx", "_test.py"))
            or "vitest" in f
            or "jest" in f
            or "spec." in f
            for f in files_l
        )
        if has_pkg:
            return "npm test" if has_test_infra else "npm run build"
        if any(f.endswith(".py") for f in files_l):
            return "pytest -q"
        if any(f.endswith(".go") for f in files_l):
            return "go test ./..."
        return None

    async def execute(self, node: TaskNode, ctx: WorkerContext) -> dict:
        project_id = str(node.params.get("project_id") or "")
        if not project_id:
            return {
                "success": False,
                "error": "缺少 project_id",
                "error_code": "INVALID_ARGS",
            }

        files = await list_project_files(ctx.user_id, project_id)
        requested = str(node.params.get("command") or "").strip()
        command = await self._pick_command(project_id, files, requested)
        if not command:
            return {
                "success": True,
                "tests_passed": False,
                "command": None,
                "error": "无法识别项目类型，请人工指定验证命令（如 npm run build / pytest -q）",
                "project_files": files[:20],
            }

        # 执行一次（信任项目免确认；未信任则由用户确认）
        await _report_progress(ctx.job_id, node.id, f"正在执行 {command}…")
        run = await self.run_skill(
            "run_project_command", {"project_id": project_id, "command": command}, ctx
        )
        ok = bool(run.get("success"))
        output = str(run.get("content") or "")
        error = str(run.get("error") or "")
        # 如实汇报执行结果（success=True 表示测试已执行；tests_passed 表示是否通过），
        # 避免 DAG 对命令失败做重试（重试会再次发起确认，造成重复弹窗）。
        return {
            "success": True,
            "tests_passed": ok,
            "command": command,
            "output": (output or error or "（无输出）")[:4000],
            "error": None if ok else (error[:500] or "命令执行失败"),
            "error_code": None if ok else (run.get("error_code") or "EXEC_ERROR"),
            "step_title": f"测试 {command}：{'通过' if ok else '未通过'}",
        }
