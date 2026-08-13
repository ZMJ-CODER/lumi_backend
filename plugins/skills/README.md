# 技能插件目录

按八大类分目录存放，每个 `.py` 文件都是一个技能插件（下划线开头会被跳过）：

| 目录 | 分类 | 现有工具 |
| --- | --- | --- |
| `filesystem/` | 文件系统操作 | list_directory / read_file / write_file / edit_file / search_files / file_stat / list_project / read_project_file / write_project_file |
| `shell/` | 终端/shell 执行 | run_project_command / python_exec / bash（超时+输出限制，高危确认） |
| `process/` | 进程管理 | ps / kill（高危确认） |
| `system/` | 系统信息与硬件 | get_datetime / env |
| `network/` | 网络与 web 工具 | web_search / query_knowledge（信息检索）/ curl |
| `devtools/` | 开发工具链 | git（status/diff/commit，commit 确认）/ explore_project（项目结构/技术栈）/ search_codebase（语义搜代码）/ create_task_plan（需求→Task DAG）/ review_code（代码审查）/ lint_code（自动识别 linter）/ run_tests（自动识别测试命令）/ generate_tests（生成测试用例）/ analyze_logs（日志根因分析）/ example（add_numbers） |
| `desktop/` | GUI 与桌面控制 | open_file / ask_user（人工提问，最重要） |
| `mcp/` | MCP 生态工具 | （待实现） |

## 文件格式

```python
from app.agents.skills.base import Skill, SkillResult

class MySkill(Skill):
    name = "my_skill"
    description = "技能做什么、什么时候用"
    category = "filesystem"         # filesystem / shell / process / system / network / devtools / desktop / mcp
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

参考 [devtools/example.py](./devtools/example.py)。

## 热更新

- 启动时自动加载本目录全部插件；
- 加载器递归扫描子目录（每类一个目录），模块名按"分类_文件名"生成，避免重名冲突；
- 修改/新增后调用 `POST /api/v1/admin/skills/reload`（管理员）即可生效，无需重启进程；
- 同名插件会覆盖内置技能（卸载时自动恢复内置版本）；
- Docker 部署时 `./plugins` 已挂载为 volume，改文件无需重建镜像。

## 安全边界

插件在服务端进程内直接执行 Python 代码，属于**受信代码**（仅管理员放置），
不能作为用户上传入口；用户级插件市场需走沙箱隔离（后续支持）。
