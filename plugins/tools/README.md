# 技能插件目录

原子工具与组合 Skill 分开管理：本目录的 `Tool` 进入 `ToolRegistry`；
开发者公共工作流放在相邻的 `../workflows/developer/`，进入 `SkillRegistry`，
不会出现在模型的 Function Calling 候选里。用户自建 Skill 不写文件，只以
声明式步骤保存到数据库，并按 `user_id` 私有隔离。

| 目录 | 分类 | 说明 |
| --- | --- | --- |
| `core/` | 规范基础工具 | 18 个稳定原子工具；模型公共候选池的唯一来源。 |
| `office/` | 办公领域原子能力 | 由已编译的办公节点或 Workflow Skill 调用，注册为内部工具，不参与通用候选。 |
| `network/` | 领域检索能力 | `query_knowledge` 等保留为内部实现；只有规范基础工具可进入公共候选。 |
| `shell/`、`devtools/` | 专用开发能力 | 仅代码 Worker 或受控 Workflow 调用。 |

## 文件格式

```python
from app.agents.skills.base import Tool, SkillResult

class MyTool(Tool):
    name = "my_skill"
    description = "技能做什么、什么时候用"
    category = "filesystem"         # filesystem / shell / process / system / network / devtools / desktop / mcp
    environment = "server"          # server（后端执行）/ sandbox（隔离沙箱）/ client（用户端执行）
    permission = "user"             # user / admin
    requires_confirmation = False   # True = 高危，执行前需用户确认
    scenes = ["chat", "office"]     # 可用场景白名单，空 = 全场景
    resource = "document"            # 根资源，用于 L1 域分组
    domain = "document"              # 域策略键，描述来自 config/agent_policies/tool_domains.yaml
    use_when = ["用户明确要求读取已授权文档"]
    do_not_use_when = ["用户要求联网搜索"]
    action_type = "read"             # read / write（写操作必须声明幂等语义）
    parameters_schema = {           # JSON Schema（LLM function calling 参数校验）
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "required": ["x"],
    }

    async def execute(self, params: dict, context=None) -> SkillResult:
        return SkillResult(success=True, output="结果")
```

可参考现有 `devtools/` 下的具体工具实现。

## 热更新

- 启动时自动加载本目录全部插件；
- 加载器递归扫描子目录（每类一个目录），模块名按"分类_文件名"生成，避免重名冲突；
- 修改/新增后调用 `POST /api/v1/admin/skills/reload`（管理员）即可生效，无需重启进程；
- 同名插件会覆盖内置技能（卸载时自动恢复内置版本）；
- Docker 部署时 `./plugins` 已挂载为 volume，改文件无需重建镜像。

## 安全边界

本目录的插件在服务端进程内直接执行 Python，属于**受信代码**，仅开发者可维护。
前端用户不能上传 Python 插件；用户 Skill 仅可使用 `workflow-skills` 接口保存
已经注册 Tool 的白名单与声明式步骤。
