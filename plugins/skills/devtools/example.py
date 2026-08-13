"""技能插件示例（devtools/开发工具链）—— 复制本文件并修改即可新增一个技能.

要求：
  - 文件名 = 模块名（仅字母/数字/下划线），以下划线开头会被跳过
  - 文件内定义一个继承 Skill 的类（可定义多个）
  - 类属性：name / description / category / environment / permission /
            requires_confirmation / scenes / parameters_schema
  - 实现 async execute(params, context) -> SkillResult

热更新：
  1. 修改/新增文件后，调 POST /admin/skills/reload（管理员）
  2. Docker 部署时 plugins 目录已挂载为 volume，改文件无需重建镜像
"""

from app.agents.skills.base import Skill, SkillResult


class AddNumbersSkill(Skill):
    name = "add_numbers"
    description = "计算两个数字的和。"
    category = "devtools"
    environment = "server"          # server / sandbox / client
    permission = "user"             # user / admin
    requires_confirmation = False   # 高危操作置 True（client 技能由用户端弹窗确认）
    scenes = ["chat", "office"]     # 空列表 = 全场景
    parameters_schema = {
        "type": "object",
        "properties": {
            "a": {"type": "number", "description": "第一个数字"},
            "b": {"type": "number", "description": "第二个数字"},
        },
        "required": ["a", "b"],
    }

    async def execute(self, params: dict, context=None) -> SkillResult:
        try:
            a = float(params.get("a"))
            b = float(params.get("b"))
        except (TypeError, ValueError):
            return SkillResult(
                success=False,
                error="参数 a/b 必须是数字",
                error_code="INVALID_ARGS",
                retryable=False,
            )
        result = a + b
        return SkillResult(success=True, output=f"{a} + {b} = {result:g}")
