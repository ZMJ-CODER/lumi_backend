# 技能插件目录

放在本目录下的每个 `.py` 文件都是一个技能插件（下划线开头会被跳过）。

## 文件格式

```python
from app.agents.skills.base import Skill, SkillResult

class MySkill(Skill):
    name = "my_skill"
    description = "技能做什么、什么时候用"
    category = "computation"        # data_query / web / computation / knowledge / system_op / client_op
    environment = "server"          # server（后端执行）/ sandbox（隔离沙箱）/ client（用户端执行）
    permission = "user"             # user / admin
    requires_confirmation = False   # True = 高危，执行前需用户确认
    scenes = ["chat", "office"]     # 可用场景白名单，空 = 全场景
    parameters_schema = {           # JSON Schema（LLM function calling 参数校验）
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "required": ["x"],
    }

    async def execute(self, params: dict, context=None) -> SkillResult:
        return SkillResult(success=True, output="结果")
```

参考 [example_skill.py](./example_skill.py)。

## 热更新

- 启动时自动加载本目录全部插件；
- 修改/新增后调用 `POST /api/v1/admin/skills/reload`（管理员）即可生效，无需重启进程；
- 同名插件会覆盖内置技能（卸载时自动恢复内置版本）；
- Docker 部署时 `./plugins` 已挂载为 volume，改文件无需重建镜像。

## 安全边界

插件在服务端进程内直接执行 Python 代码，属于**受信代码**（仅管理员放置），
不能作为用户上传入口；用户级插件市场需走沙箱隔离（后续支持）。
