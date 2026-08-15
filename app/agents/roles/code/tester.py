"""测试 agent：按项目类型自动选择验证命令（构建/测试，能正常退出）并如实汇报结果."""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from app.agents.core.base import WorkerAgent, WorkerContext
from app.agents.core.progress import set_progress as _report_progress
from app.agents.core.tools import TEST_SYSTEM_PROMPT, list_project_files

if TYPE_CHECKING:
    from app.agents.orchestration.models import TaskNode


class CodeTesterAgent(WorkerAgent):
    """测试 agent：按项目类型自动选择验证命令（构建/测试，能正常退出）并如实汇报结果.

    注意：dev 服务器不会退出，不适合自动验证；简单前端项目用 npm run build 校验即可。
    执行结果以 success=True + tests_passed 汇报，避免命令失败触发 DAG 重试导致重复确认弹窗。
    """

    name = "code_tester"
    description = "在本地项目自动选择并运行合适的测试/构建命令，如实汇报结果"
    params_help = 'params 用 {"project_id": "项目ID"}，不要预设 command，由 tester 根据项目文件自行决定'
    skills = [
        "read_project_file",
        "write_project_file",
        "run_in_sandbox",
        "check_new_dependencies",
        "install_new_dependencies",
        "rollback_dependency_manifests",
        "run_static_check",
    ]

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

        # 生成代码可能新增依赖：测试前先检测并安装（"帮我确认"开启时自动装，否则弹窗确认）
        deps_installed = False
        try:
            check = await self.run_skill(
                "check_new_dependencies", {"project_id": project_id}, ctx
            )
            if check.get("success") and check.get("changed"):
                deps = check.get("deps") or []
                await _report_progress(
                    ctx.job_id, node.id, f"检测到新增依赖 {len(deps)} 项，正在安装…"
                )
                inst = await self.run_skill(
                    "install_new_dependencies",
                    {"project_id": project_id, "deps": deps},
                    ctx,
                )
                if not inst.get("success") or not inst.get("installed"):
                    msg = str(inst.get("error") or "新增依赖安装失败")
                    return {
                        "success": True,
                        "tests_passed": False,
                        "command": command,
                        "output": msg[:4000],
                        "error": f"新增依赖安装失败: {msg[:500]}",
                        "error_code": "DEP_INSTALL_FAILED",
                        "step_title": f"测试 {command}：未通过（依赖安装失败）",
                    }
                deps_installed = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Tester] 依赖检查/安装异常，继续执行测试: {}", exc)

        # 轻量级静态检查前置：秒级发现编译/类型错误，替代"先等完整构建"。
        # 静态通过后完整测试异步进行（提交阶段），这里不再阻塞等待构建。
        await _report_progress(ctx.job_id, node.id, "正在运行静态类型检查…")
        static = await self.run_skill(
            "run_static_check", {"project_id": project_id}, ctx
        )
        static_passed = static.get("passed")
        if static_passed is False:
            out = str(static.get("output") or static.get("error") or "静态检查未通过")[:4000]
            if deps_installed:
                try:
                    await self.run_skill(
                        "rollback_dependency_manifests", {"project_id": project_id}, ctx
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[Tester] 依赖清单回滚失败: {}", exc)
            return {
                "success": True,
                "tests_passed": False,
                "command": str(static.get("command") or "静态检查"),
                "output": out,
                "error": "静态类型检查未通过",
                "error_code": "STATIC_CHECK_FAILED",
                "step_title": f"静态检查 {static.get('command') or ''}：未通过",
            }
        if static_passed is True:
            return {
                "success": True,
                "tests_passed": True,
                "command": str(static.get("command") or "静态检查"),
                "output": "静态类型检查通过（完整测试将在提交阶段异步进行）",
                "error": None,
                "error_code": None,
                "static_ok": True,
                "step_title": f"静态检查 {static.get('command') or ''}：通过",
            }

        # 项目无可用的静态检查器：回退到完整构建/测试（原逻辑）
        # 在沙箱副本中执行（暂存修改已应用，不触碰真实项目）
        await _report_progress(ctx.job_id, node.id, f"正在沙箱中执行 {command}…")
        run = await self.run_skill(
            "run_in_sandbox", {"project_id": project_id, "command": command}, ctx
        )
        ok = bool(run.get("success"))
        output = str(run.get("content") or "")
        error = str(run.get("error") or "")
        # 安装过新依赖但测试仍不过：回滚本次任务改动的依赖清单，别把用户项目搞脏
        if not ok and deps_installed:
            try:
                await self.run_skill(
                    "rollback_dependency_manifests", {"project_id": project_id}, ctx
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Tester] 依赖清单回滚失败: {}", exc)
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
