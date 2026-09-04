# Workflow Skill 目录

此目录只存放开发者维护的公共组合流程：

- `developer/`：受信 Python Workflow Skill，启动时加载，对所有用户可见；
- 每个 Workflow Skill 用 `allowed_tools` 声明其唯一可调用的原子 Tool 集合；
- Workflow Skill 不进入 Function Calling 候选池，只能由规划器生成 `workflow_skill` 节点后执行；
- 不要把原子 Tool 放进本目录。加载器会拒绝类型与目录不一致的文件。

用户创建的 Workflow Skill 不写入本目录，也不允许上传 Python。它们通过
`/api/v1/workflow-skills` 保存为数据库中的声明式步骤，按 `user_id` 默认私有隔离。
用户流程仅能引用标记为 `user_workflow_allowed = True` 的稳定只读 Tool。

## 新增公共 Workflow Skill

```python
from app.agents.skills.base import WorkflowSkill, ToolOutput


class ExampleWorkflow(WorkflowSkill):
    name = "example_workflow"
    description = "组合多个受控 Tool 完成一个明确流程。"
    scenes = ["office"]
    allowed_tools = ["calculator"]

    async def run(self, params, context, invoke_tool) -> ToolOutput:
        result = await invoke_tool("calculator", {"expression": "1 + 1"})
        return result
```

内部调用必须使用注入的 `invoke_tool`，不得直接调用 `Tool.execute`。
