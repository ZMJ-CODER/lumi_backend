# 基础工具迁移

项目将原子能力收束为 18 个规范工具，组合流程仍放在 `plugins/workflows`。

## 开关

当前默认已启用基础协议；旧工具仅供已存在的 Workflow/Temporal 快照恢复。若需
临时回退兼容模式，可在 `.env` 设置：

```env
AGENT_BASE_TOOLS_ONLY=true
```

开启时，Function Calling、ReAct 候选池、计划编译和外部 MCP 候选都会只允许
`config/agent_policies/base_tools.yaml` 中的名称。旧工具文件仍可被 Workflow
内部显式引用，避免历史任务恢复时出现工具不存在。

## 迁移规则

`read_file/write_file/edit_file/search_files/grep_code/bash/ask_user/web_search`
只在边界层映射为 `Read/Write/Edit/Glob/Grep/Bash/AskUserQuestion/WebSearch`。
业务节点不应再新增旧名称。`TodoWrite` 使用显式 `todos` 数组，不自动猜测旧的
`add/list/complete/delete` 动作。

文件写入遵循“先 Read，再 Edit/Write”并校验读取后的文件指纹；Bash 后台任务使用
`BashOutput` 读取增量输出、`KillShell` 终止会话。

项目专用工具也在客户端队列边界统一：`list_project→Glob`、
`read_project_file→Read`、`write_project_file→Write`、
`run_project_command→Bash`，`project_id` 仅用于授权范围校验。
