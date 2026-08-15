"""技能插件（filesystem/文件系统操作）：客户端本地文件技能 —— 由用户端（Electron）执行.

执行通道（混合架构）：配置 MCP 时由后端直连 Electron 端 MCP server 执行；
未配置/连接失败时回退 Redis 轮询（创建待执行请求 → 用户端轮询 → 高危弹窗确认 → 执行 → 回传结果）。
本模块：list_directory / read_file / write_file（open_file 归入 desktop 分类）。
"""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


async def _run_client_skill(
    user_id: str,
    skill_name: str,
    params: dict,
    requires_confirmation: bool,
) -> SkillResult:
    """MCP 优先、Redis 轮询兜底的统一客户端技能执行."""
    return await run_client_skill_request(
        user_id,
        skill_name,
        params,
        requires_confirmation=requires_confirmation,
    )


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class ListDirectorySkill(Skill):
    name = "list_directory"
    description = (
        "列出用户电脑上某个目录下的文件和子目录（含名称、类型、大小）。"
        "当用户询问本地文件夹/目录里有什么时使用。"
    )
    category = "filesystem"
    environment = "client"
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目录的绝对路径"},
            "include_hidden": {"type": "boolean", "description": "是否包含隐藏文件（默认 false）"},
        },
        "required": ["path"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        path = str(params.get("path") or "").strip()
        if not path:
            return SkillResult(success=False, error="缺少目录路径 path", error_code="INVALID_ARGS", retryable=False)
        _notify(context, f"（正在查看本地目录：{path}）")
        return await _run_client_skill(
            context.user_id if context else "",
            self.name,
            {"path": path, "include_hidden": bool(params.get("include_hidden"))},
            False,
        )


class ReadFileSkill(Skill):
    name = "read_file"
    description = (
        "读取用户电脑上某个文本文件的完整内容（单次最大 200KB，超出截断）。"
        "当用户让你查看、总结或分析本地文件内容时使用。"
    )
    category = "filesystem"
    environment = "client"
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件绝对路径"},
            "max_chars": {"type": "integer", "description": "最多读取字符数（默认 200000）", "minimum": 100, "maximum": 200000},
        },
        "required": ["path"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        path = str(params.get("path") or "").strip()
        if not path:
            return SkillResult(success=False, error="缺少文件路径 path", error_code="INVALID_ARGS", retryable=False)
        _notify(context, f"（正在读取本地文件：{path}）")
        return await _run_client_skill(
            context.user_id if context else "",
            self.name,
            {"path": path, "max_chars": int(params.get("max_chars") or 200000)},
            False,
        )


class WriteFileSkill(Skill):
    name = "write_file"
    description = (
        "把文本内容写入用户电脑的指定文件（会覆盖已有内容，目录不存在时自动创建）。"
        "高危操作：执行前需要用户在客户端确认。"
    )
    category = "filesystem"
    environment = "client"
    permission = "user"
    requires_confirmation = True
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要写入的文件绝对路径"},
            "content": {"type": "string", "description": "要写入的完整文本内容"},
        },
        "required": ["path", "content"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        path = str(params.get("path") or "").strip()
        content = str(params.get("content") or "")
        if not path:
            return SkillResult(success=False, error="缺少文件路径 path", error_code="INVALID_ARGS", retryable=False)
        _notify(context, f"（正在请求写入本地文件：{path}，请在弹出的确认框中确认）")
        return await _run_client_skill(
            context.user_id if context else "",
            self.name,
            {"path": path, "content": content},
            True,
        )
