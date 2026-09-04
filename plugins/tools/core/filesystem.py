"""本机文件基础工具：Read、Edit、Write 与 Glob。"""

from app.agents.skills.base import SkillContext, Tool, ToolOutput
from app.agents.skills.executor import run_client_skill_request


async def _client(name: str, params: dict, context: SkillContext | None, confirm: bool = False) -> ToolOutput:
    if not context or not context.user_id:
        return ToolOutput(success=False, error="需要登录后使用", error_code="AUTH_REQUIRED", retryable=False)
    return await run_client_skill_request(context.user_id, name, params, confirm)


class ReadTool(Tool):
    name = "Read"
    description = "读取用户已授权的本机文本文件；不会读取未授权路径或把全文自动复制到最终回复。"
    category = "filesystem"
    domain = "filesystem"
    resource = "filesystem"
    environment = "client"
    parameters_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "文件绝对路径"},
            "offset": {"type": "integer", "minimum": 0, "description": "起始字符位置"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 2_000_000, "description": "最多读取字符数"},
        },
        "required": ["file_path"],
    }
    use_when = ["用户明确要求查看或分析已授权文件"]
    do_not_use_when = ["目标路径未授权", "需要修改文件时应使用 Edit 或 Write"]
    selection_examples = ["读取项目中的 config.yaml → Read"]
    result_contract = "返回截断后的文本和文件元数据。"

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        path = str(params.get("file_path") or "").strip()
        if not path:
            return ToolOutput(success=False, error="缺少 file_path", error_code="INVALID_ARGS", retryable=False)
        payload = {"file_path": path, "offset": int(params.get("offset") or 0), "limit": int(params.get("limit") or 200000)}
        if params.get("project_id"):
            payload["project_id"] = str(params["project_id"])
        result = await _client("Read", payload, context)
        return result


class EditTool(Tool):
    name = "Edit"
    description = "精确替换已授权文本文件中的片段；执行前必须先完成 Read，避免覆盖错误版本。"
    category = "filesystem"
    domain = "filesystem"
    resource = "filesystem"
    environment = "client"
    requires_confirmation = True
    write_op = True
    parameters_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "文件绝对路径"},
            "old_string": {"type": "string", "description": "文件中必须唯一匹配的原文"},
            "new_string": {"type": "string", "description": "替换后的文本"},
            "replace_all": {"type": "boolean", "default": False},
        },
        "required": ["file_path", "old_string", "new_string"],
    }
    use_when = ["用户明确要求修改已授权文件"]
    do_not_use_when = ["尚未读取目标文件", "用户未授权写入"]
    result_contract = "返回替换数量和文件路径。"

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        path = str(params.get("file_path") or "").strip()
        old = str(params.get("old_string") or "")
        if not path or not old:
            return ToolOutput(success=False, error="缺少 file_path 或 old_string", error_code="INVALID_ARGS", retryable=False)
        payload = {"file_path": path, "old_string": old, "new_string": str(params.get("new_string") or ""), "replace_all": bool(params.get("replace_all"))}
        if params.get("project_id"):
            payload["project_id"] = str(params["project_id"])
        return await _client("Edit", payload, context, True)


class WriteTool(Tool):
    name = "Write"
    description = "将文本写入已授权的本机文件；覆盖已有文件前必须先 Read，并需要用户确认。"
    category = "filesystem"
    domain = "filesystem"
    resource = "filesystem"
    environment = "client"
    requires_confirmation = True
    write_op = True
    parameters_schema = {
        "type": "object",
        "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["file_path", "content"],
    }
    use_when = ["用户明确要求创建或覆盖已授权文件"]
    do_not_use_when = ["用户未授权写入", "只需要查看文件时应使用 Read"]
    result_contract = "返回写入字节数和文件路径。"

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        path = str(params.get("file_path") or "").strip()
        if not path:
            return ToolOutput(success=False, error="缺少 file_path", error_code="INVALID_ARGS", retryable=False)
        payload = {"file_path": path, "content": str(params.get("content") or "")}
        if params.get("project_id"):
            payload["project_id"] = str(params["project_id"])
        return await _client("Write", payload, context, True)


class GlobTool(Tool):
    name = "Glob"
    description = "在用户明确授权的目录中按文件名模式查找文件。"
    category = "filesystem"
    domain = "filesystem"
    resource = "filesystem"
    environment = "client"
    parameters_schema = {
        "type": "object",
        "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 200}},
        "required": ["pattern"],
    }
    use_when = ["用户要求按文件名或通配符搜索文件"]
    do_not_use_when = ["需要搜索文件内容时应使用 Grep", "目录未授权"]
    result_contract = "返回匹配文件路径列表。"

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        pattern = str(params.get("pattern") or "").strip()
        path = str(params.get("path") or ".").strip()
        if not pattern:
            return ToolOutput(success=False, error="缺少 pattern", error_code="INVALID_ARGS", retryable=False)
        payload = {"pattern": pattern, "path": path, "max_results": int(params.get("max_results") or 50)}
        if params.get("project_id"):
            payload["project_id"] = str(params["project_id"])
        return await _client("Glob", payload, context)


class NotebookEditTool(Tool):
    """编辑 notebook 单元格；具体 JSON 解析留在用户设备侧完成。"""

    name = "NotebookEdit"
    description = "编辑已授权 Jupyter notebook 的指定单元格；执行前必须先 Read。"
    category = "filesystem"
    domain = "filesystem"
    resource = "filesystem"
    environment = "client"
    requires_confirmation = True
    write_op = True
    parameters_schema = {
        "type": "object",
        "properties": {
            "notebook_path": {"type": "string"},
            "cell_index": {"type": "integer", "minimum": 0},
            "new_source": {"type": "string"},
            "edit_mode": {"type": "string", "enum": ["replace", "insert", "delete"]},
        },
        "required": ["notebook_path", "cell_index", "edit_mode"],
    }
    use_when = ["用户明确要求编辑 ipynb notebook 单元格"]
    do_not_use_when = ["非 ipynb 文件", "尚未读取 notebook", "用户未授权写入"]
    result_contract = "返回修改的单元格索引和 notebook 路径。"

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        target = str(params.get("notebook_path") or "").strip()
        if not target:
            return ToolOutput(success=False, error="缺少 notebook_path", error_code="INVALID_ARGS", retryable=False)
        payload = {
            "notebook_path": target,
            "cell_index": params.get("cell_index"),
            "new_source": str(params.get("new_source") or ""),
            "edit_mode": str(params.get("edit_mode") or ""),
        }
        if params.get("project_id"):
            payload["project_id"] = str(params["project_id"])
        return await _client("NotebookEdit", payload, context, True)
