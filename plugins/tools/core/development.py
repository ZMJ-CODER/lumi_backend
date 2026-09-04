"""代码搜索基础工具。"""

from app.agents.skills.base import SkillContext, Tool, ToolOutput
from app.agents.skills.executor import run_client_skill_request


class GrepTool(Tool):
    name = "Grep"
    description = "在已授权项目或目录中搜索文本内容，返回文件、行号和短片段。"
    category = "development"
    domain = "filesystem"
    resource = "project"
    environment = "client"
    parameters_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "搜索文本或正则"},
            "path": {"type": "string", "description": "已授权项目 ID 或目录"},
            "glob": {"type": "string", "description": "文件过滤模式"},
            "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"]},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["pattern", "path"],
    }
    use_when = ["用户要求定位代码、配置或日志中的内容"]
    do_not_use_when = ["只按文件名搜索时应使用 Glob", "路径未授权"]
    result_contract = "返回受限数量的匹配文件、行号和片段。"

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        if not context or not context.user_id:
            return ToolOutput(success=False, error="需要登录后使用", error_code="AUTH_REQUIRED", retryable=False)
        pattern = str(params.get("pattern") or "").strip()
        path = str(params.get("path") or "").strip()
        if not pattern or not path:
            return ToolOutput(success=False, error="缺少 pattern 或 path", error_code="INVALID_ARGS", retryable=False)
        return await run_client_skill_request(context.user_id, "Grep", {
            "pattern": pattern, "path": path, "glob": str(params.get("glob") or ""),
            "output_mode": str(params.get("output_mode") or "content"),
            "max_results": int(params.get("max_results") or 30),
            **({"project_id": str(params["project_id"])} if params.get("project_id") else {}),
        })
