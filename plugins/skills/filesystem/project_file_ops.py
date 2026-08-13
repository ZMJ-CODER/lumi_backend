"""技能插件（filesystem/文件系统操作）：本地代码项目文件操作 —— 由用户端（Electron）执行.

方案 A 的核心：代码留在本地，服务器 agent 通过 client 通道下发指令，
Electron 端按 project_id → 本地根路径映射 + 相对路径 jail 校验后执行。
本模块：list_project / read_project_file / write_project_file（run_project_command 归入 shell 分类）。
"""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class ListProjectSkill(Skill):
    name = "list_project"
    description = (
        "列出本地代码项目中某个目录下的文件和子目录（相对项目根路径）。"
        "当需要了解项目结构、定位文件时使用。"
    )
    category = "filesystem"
    environment = "client"
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "本地项目 ID"},
            "path": {"type": "string", "description": "相对项目根的目录路径（默认根目录）"},
            "include_hidden": {"type": "boolean", "description": "是否包含隐藏文件（默认 false）"},
        },
        "required": ["project_id"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        project_id = str(params.get("project_id") or "")
        rel = str(params.get("path") or "")
        _notify(context, f"（正在查看项目目录：{rel or '/'}）")
        return await run_client_skill_request(
            context.user_id,
            self.name,
            {
                "project_id": project_id,
                "path": rel,
                "include_hidden": bool(params.get("include_hidden")),
            },
            False,
        )


class ReadProjectFileSkill(Skill):
    name = "read_project_file"
    description = (
        "读取本地代码项目中的某个文件（相对项目根路径，单次 ≤200KB）。"
        "当需要查看代码内容来理解或修改时使用。"
    )
    category = "filesystem"
    environment = "client"
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "本地项目 ID"},
            "path": {"type": "string", "description": "相对项目根的文件路径"},
            "file_key": {"type": "string", "description": "相对路径哈希（可选；path 缺失时使用，客户端按本地映射还原）"},
            "max_chars": {"type": "integer", "description": "最多读取字符数（默认 200000）", "minimum": 100, "maximum": 200000},
        },
        "required": ["project_id"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        project_id = str(params.get("project_id") or "")
        rel = str(params.get("path") or "")
        file_key = str(params.get("file_key") or "")
        if not rel and not file_key:
            return SkillResult(
                success=False, error="缺少 path 或 file_key", error_code="INVALID_ARGS", retryable=False
            )
        _notify(context, f"（正在读取项目文件：{rel or file_key}）")
        payload = {"project_id": project_id, "max_chars": int(params.get("max_chars") or 200000)}
        if rel:
            payload["path"] = rel
        else:
            payload["file_key"] = file_key
        return await run_client_skill_request(
            context.user_id,
            self.name,
            payload,
            False,
        )


class WriteProjectFileSkill(Skill):
    name = "write_project_file"
    description = (
        "把文本内容写入本地代码项目中的文件（相对项目根，覆盖已有内容，自动创建目录）。"
        "高危操作：执行前需要用户在客户端确认。"
    )
    category = "filesystem"
    environment = "client"
    requires_confirmation = True
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "本地项目 ID"},
            "path": {"type": "string", "description": "相对项目根的文件路径"},
            "file_key": {"type": "string", "description": "相对路径哈希（可选；path 缺失时使用）"},
            "content": {"type": "string", "description": "要写入的完整文件内容"},
        },
        "required": ["project_id", "content"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        project_id = str(params.get("project_id") or "")
        rel = str(params.get("path") or "")
        file_key = str(params.get("file_key") or "")
        content = str(params.get("content") or "")
        if not rel and not file_key:
            return SkillResult(
                success=False, error="缺少 path 或 file_key", error_code="INVALID_ARGS", retryable=False
            )
        _notify(context, f"（正在请求写入项目文件：{rel or file_key}，请在弹出的确认框中确认）")
        payload = {"project_id": project_id, "content": content}
        if rel:
            payload["path"] = rel
        else:
            payload["file_key"] = file_key
        return await run_client_skill_request(
            context.user_id,
            self.name,
            payload,
            True,
        )

