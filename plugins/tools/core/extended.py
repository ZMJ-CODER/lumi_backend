"""扩展基础工具：补齐文件元数据、进程控制、桌面操作和确定性系统能力。"""

from app.agents.skills.base import SkillContext, Tool, ToolOutput
from app.agents.skills.executor import run_client_skill_request


class FileStatTool(Tool):
    name = "FileStat"
    description = "读取已授权文件或目录的大小、类型和时间等元数据，不读取正文。"
    category = "filesystem"
    domain = "filesystem"
    resource = "filesystem"
    environment = "client"
    parameters_schema = {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}
    use_when = ["用户询问文件或目录元数据"]
    do_not_use_when = ["需要读取正文时使用 Read", "路径未授权"]
    result_contract = "返回结构化文件元数据。"

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        return await run_client_skill_request(
            context.user_id if context else "", "FileStat", {"file_path": str(params.get("file_path") or "")}, False
        )


class RenameTool(Tool):
    name = "Rename"
    description = "重命名已授权文件或目录；必须明确源路径和目标路径。"
    category = "filesystem"
    domain = "filesystem"
    resource = "filesystem"
    environment = "client"
    requires_confirmation = True
    write_op = True
    parameters_schema = {"type": "object", "properties": {"file_path": {"type": "string"}, "new_path": {"type": "string"}}, "required": ["file_path", "new_path"]}
    use_when = ["用户明确要求重命名文件或目录"]
    do_not_use_when = ["未明确源路径和目标路径", "用户未授权写入"]
    result_contract = "返回重命名后的路径。"

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        return await run_client_skill_request(context.user_id if context else "", "Rename", {"file_path": str(params.get("file_path") or ""), "new_path": str(params.get("new_path") or "")}, True)


class DeleteTool(Tool):
    name = "Delete"
    description = "删除已授权文件或目录；操作可恢复时优先移入回收站。"
    category = "filesystem"
    domain = "filesystem"
    resource = "filesystem"
    environment = "client"
    requires_confirmation = True
    write_op = True
    parameters_schema = {"type": "object", "properties": {"file_path": {"type": "string"}, "recursive": {"type": "boolean"}}, "required": ["file_path"]}
    use_when = ["用户明确要求删除文件或目录"]
    do_not_use_when = ["目标不明确", "未明确要求递归删除"]
    result_contract = "返回删除状态。"

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        return await run_client_skill_request(context.user_id if context else "", "Delete", {"file_path": str(params.get("file_path") or ""), "recursive": bool(params.get("recursive"))}, True)


class ProcessListTool(Tool):
    name = "ProcessList"
    description = "查看用户电脑上的运行进程，可按名称过滤。"
    category = "process"
    domain = "system"
    resource = "process"
    environment = "client"
    parameters_schema = {"type": "object", "properties": {"pattern": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 200}}}
    use_when = ["用户要求查看本机运行进程"]
    do_not_use_when = ["需要终止进程时使用 ProcessSignal"]
    result_contract = "返回进程名、PID 和内存占用。"

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        return await run_client_skill_request(context.user_id if context else "", "ProcessList", {"pattern": str(params.get("pattern") or ""), "max_results": int(params.get("max_results") or 50)}, False)


class ProcessSignalTool(Tool):
    name = "ProcessSignal"
    description = "向指定进程发送终止信号；可能导致未保存数据丢失。"
    category = "process"
    domain = "system"
    resource = "process"
    environment = "client"
    requires_confirmation = True
    write_op = True
    parameters_schema = {"type": "object", "properties": {"pid": {"type": "integer"}, "name": {"type": "string"}, "force": {"type": "boolean"}}}
    use_when = ["用户明确要求终止指定进程"]
    do_not_use_when = ["未指定 pid 或 name", "未获得用户确认"]
    result_contract = "返回进程信号执行结果。"

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        return await run_client_skill_request(context.user_id if context else "", "ProcessSignal", {"pid": params.get("pid"), "name": str(params.get("name") or ""), "force": bool(params.get("force", True))}, True)


class CalculatorTool(Tool):
    name = "Calculator"
    description = "安全、确定性地计算数字和括号算术表达式。"
    category = "system"
    domain = "system"
    resource = "compute"
    environment = "server"
    deterministic = True
    parameters_schema = {"type": "object", "properties": {"expression": {"type": "string"}, "precision": {"type": "integer", "minimum": 0, "maximum": 12}}, "required": ["expression"]}
    use_when = ["用户要求精确算术计算"]
    do_not_use_when = ["需要执行任意代码", "需要读取或修改文件"]
    result_contract = "返回表达式和确定性结果。"

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        from plugins.tools.core.calculator import execute_calculator

        return await execute_calculator(params, context)


class DateTimeTool(Tool):
    name = "DateTime"
    description = "获取当前东八区日期、时间或星期。"
    category = "system"
    domain = "system"
    resource = "clock"
    environment = "server"
    deterministic = False
    parameters_schema = {"type": "object", "properties": {"format": {"type": "string", "enum": ["date", "time", "datetime"]}}, "required": ["format"]}
    use_when = ["用户询问当前日期或时间"]
    do_not_use_when = ["询问历史日期或用户日程"]
    result_contract = "返回当前东八区时间。"

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        from plugins.tools.core.clock import execute_datetime

        return await execute_datetime(params, context)


class SystemInfoTool(Tool):
    name = "SystemInfo"
    description = "读取受限的客户端系统信息，不返回密钥或完整环境变量。"
    category = "system"
    domain = "system"
    resource = "system"
    environment = "client"
    parameters_schema = {"type": "object", "properties": {"keys": {"type": "array", "items": {"type": "string"}}}}
    use_when = ["用户询问受限系统信息"]
    do_not_use_when = ["读取密钥、凭据或未授权环境变量"]
    result_contract = "返回受限系统信息。"

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        return await run_client_skill_request(context.user_id if context else "", "SystemInfo", {"keys": list(params.get("keys") or [])}, False)


class OpenFileTool(Tool):
    name = "OpenFile"
    description = "用系统默认应用打开用户明确指定的本地文件。"
    category = "desktop"
    domain = "desktop"
    resource = "desktop"
    environment = "client"
    requires_confirmation = True
    write_op = True
    parameters_schema = {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}
    use_when = ["用户明确要求打开本地文件"]
    do_not_use_when = ["仅需读取内容时使用 Read", "未获得确认"]
    result_contract = "返回打开状态。"

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        return await run_client_skill_request(context.user_id if context else "", "OpenFile", {"file_path": str(params.get("file_path") or "")}, True)


class OpenAppTool(Tool):
    name = "OpenApp"
    description = "启动用户明确指定的本机应用。"
    category = "desktop"
    domain = "desktop"
    resource = "desktop"
    environment = "client"
    requires_confirmation = True
    write_op = True
    parameters_schema = {"type": "object", "properties": {"name": {"type": "string"}, "args": {"type": "array", "items": {"type": "string"}}}, "required": ["name"]}
    use_when = ["用户明确要求启动本机应用"]
    do_not_use_when = ["仅询问应用用法"]
    result_contract = "返回启动状态。"

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        return await run_client_skill_request(context.user_id if context else "", "OpenApp", {"name": str(params.get("name") or ""), "args": list(params.get("args") or [])}, True)


class OpenUrlTool(Tool):
    name = "OpenUrl"
    description = "用系统浏览器打开用户明确指定的 HTTP(S) 地址。"
    category = "desktop"
    domain = "desktop"
    resource = "desktop"
    environment = "client"
    requires_confirmation = True
    write_op = True
    parameters_schema = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}
    use_when = ["用户明确要求打开网页地址"]
    do_not_use_when = ["需要抓取内容时使用 WebFetch"]
    result_contract = "返回浏览器打开状态。"

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        return await run_client_skill_request(context.user_id if context else "", "OpenUrl", {"url": str(params.get("url") or "")}, True)
