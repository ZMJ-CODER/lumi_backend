"""编排器控制工具的统一声明。

这些能力不直接访问文件或网络，而是把稳定的会话控制意图交给编排层；
先以原子 Tool 契约注册，后续再接入 Temporal Signal/子代理实现。
"""

from app.agents.skills.base import SkillContext, Tool, ToolOutput


class _ControlTool(Tool):
    category = "orchestration"
    domain = "orchestration"
    resource = "session"
    environment = "server"
    deterministic = True
    use_when = ["编排器明确需要该会话控制动作"]
    do_not_use_when = ["可由普通业务工具完成时", "试图绕过权限或执行策略时"]

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        # 控制动作必须由会话编排器接管（Temporal Signal / API 状态机）。
        # 在尚未接入控制器前显式失败，禁止把“已进入计划/已创建子代理”
        # 伪装成成功结果回传给模型。
        return ToolOutput(
            success=False,
            error=f"控制工具 {self.name} 需要编排器会话控制器处理",
            error_code="CONTROL_TOOL_SERVER_ONLY",
            retryable=False,
            metadata={"action": self.name, "params": params},
        )


class TaskTool(_ControlTool):
    name = "Task"
    description = "启动受限子代理处理复杂多步任务；子代理必须一次获得完整上下文。"
    parameters_schema = {"type": "object", "properties": {"description": {"type": "string"}, "prompt": {"type": "string"}, "subagent_type": {"type": "string"}, "model": {"type": "string"}}, "required": ["description", "prompt", "subagent_type", "model"]}
    result_contract = "返回子任务标识；实际执行由编排器负责。"


class SkillTool(_ControlTool):
    name = "Skill"
    description = "按名称加载当前用户有权限使用的组合 Workflow Skill。"
    parameters_schema = {"type": "object", "properties": {"skill": {"type": "string"}}, "required": ["skill"]}
    result_contract = "返回已加载技能标识。"


class SlashCommandTool(_ControlTool):
    name = "SlashCommand"
    description = "执行服务端注册 allowlist 中的斜杠命令，不允许拼接任意命令。"
    parameters_schema = {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}
    result_contract = "返回命令路由结果。"


class EnterPlanModeTool(_ControlTool):
    name = "EnterPlanMode"
    description = "进入需要审批的计划模式。"
    parameters_schema = {"type": "object", "properties": {}}
    result_contract = "返回会话状态变更。"


class ExitPlanModeTool(_ControlTool):
    name = "ExitPlanMode"
    description = "退出计划模式并请求用户批准执行。"
    parameters_schema = {"type": "object", "properties": {}}
    result_contract = "返回会话状态变更。"

