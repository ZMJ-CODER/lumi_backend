"""技能插件（devtools/开发工具链）：explore_project —— 探索项目目录结构与技术栈."""

from app.agents.skills.base import Skill, SkillContext, SkillResult


_STACK_HINTS = {
    "python": (".py", "requirements.txt", "pyproject.toml", "setup.py"),
    "javascript": (".js", ".mjs", ".cjs"),
    "typescript": (".ts", ".tsx"),
    "vue": (".vue",),
    "react": (".jsx", ".tsx"),
    "go": (".go", "go.mod"),
    "java": (".java", "pom.xml", "build.gradle"),
    "rust": (".rs", "cargo.toml"),
    "c/c++": (".c", ".h", ".cpp", "cmakelists.txt"),
    "php": (".php", "composer.json"),
    "ruby": (".rb", "gemfile"),
    "docker": ("dockerfile", "docker-compose.yml"),
    "sql": (".sql",),
}

_ENTRY_HINTS = (
    "package.json",
    "src/main.js",
    "src/main.ts",
    "src/main.jsx",
    "src/main.tsx",
    "src/app.vue",
    "index.html",
    "main.py",
    "app.py",
    "main.go",
    "readme.md",
)


class ExploreProjectSkill(Skill):
    name = "explore_project"
    description = (
        "探索本地代码项目：列出目录结构与关键文件，识别技术栈（语言/框架/构建工具）。"
        "当需要先了解项目整体情况、确定改哪里、或判断用什么命令运行时使用。"
    )
    category = "devtools"
    environment = "server"
    requires_confirmation = False
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "本地项目 ID"},
            "max_files": {"type": "integer", "description": "最多列出文件数（默认 200）", "minimum": 10, "maximum": 2000},
        },
        "required": ["project_id"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        project_id = str(params.get("project_id") or "")
        if not project_id:
            return SkillResult(success=False, error="缺少 project_id", error_code="INVALID_ARGS", retryable=False)
        max_files = min(int(params.get("max_files") or 200), 2000)
        try:
            from app.core.database import async_session_factory
            from app.services import project_index

            async with async_session_factory() as session:
                files = await project_index.list_project_files(
                    session, context.user_id, project_id, limit=max_files
                )
        except Exception as exc:  # noqa: BLE001
            return SkillResult(
                success=False,
                error=f"探索项目失败: {exc}",
                error_code="EXEC_ERROR",
                retryable=True,
            )
        if not files:
            return SkillResult(
                success=False,
                error="项目索引为空（请先上传/建立项目索引）",
                error_code="EXEC_ERROR",
                retryable=False,
            )

        lower = [f.lower() for f in files]
        stacks = [
            name
            for name, hints in _STACK_HINTS.items()
            if any(f.endswith(h) or any(h in f for h in hints) for f in lower)
        ]
        entries = [f for f in files if f.lower() in _ENTRY_HINTS]
        # 目录概览：顶层目录 + 前 N 个文件
        dirs = sorted({f.split("/", 1)[0] for f in files if "/" in f})
        preview = files[:max_files]
        output_lines = [
            f"项目文件数：{len(files)}（显示前 {min(len(preview), 60)} 个）",
            f"技术栈：{', '.join(stacks) if stacks else '未识别'}",
            f"关键文件：{', '.join(entries) if entries else '（无）'}",
            f"顶层目录：{', '.join(dirs[:30]) if dirs else '（单层）'}",
            "文件清单：",
            *[f"- {p}" for p in preview[:60]],
        ]
        return SkillResult(
            success=True,
            output="\n".join(output_lines),
            metadata={
                "stacks": stacks,
                "key_files": entries,
                "top_dirs": dirs[:30],
                "file_count": len(files),
            },
        )
