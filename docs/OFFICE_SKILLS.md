# 办公 Skill 能力说明（历史兼容入口）

> 状态：能力名称和前端组件会随插件演进，本文件不再维护静态“功能全表”。
> 更新：2026-08-23

旧版本把“注册的 Skill”直接等同于“任意办公消息都能获得的工具”，并引用已移除的前端
组件与旧 Temporal 主路径；这会误导权限和路由行为。

当前能力模型如下：

1. `plugins/skills/` 中的 Skill 先经场景、角色、写开关、运行时可用性和用户绑定过滤；
2. 聊天仅从受限问答池按请求注入 Top-K，普通常识不会暴露工具；
3. 办公 ReAct 每轮按最新观察刷新受限候选池，仍由 Gateway 复核参数、授权、文档范围、确认和副作用 journal；
4. 多文档事实定位先 `inspect_document_set`，再以授权 `doc_id` 调用 `read_document`；
5. 新 Skill 的可用条件、排除边界、生命周期和评测要求以注册契约为准，而不是本文的静态表格。

请使用以下当前文档：

- [TOOL_SKILL_EXECUTION_GUIDE.md](TOOL_SKILL_EXECUTION_GUIDE.md)：模型如何选择/不选择工具、结果如何回填；
- [MCP_SKILL_GOVERNANCE.md](MCP_SKILL_GOVERNANCE.md)：注册契约、候选池、bootstrap、MCP 准入；
- [CURRENT_DAG_ARCHITECTURE.md](CURRENT_DAG_ARCHITECTURE.md)：办公任务中的 DAG、文档定位、审批与恢复；
- `GET /api/v1/admin/skills`：部署中实际加载的 Skill 与版本的运行时目录（管理员接口）。
