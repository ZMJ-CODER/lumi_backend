# 办公编排与 MCP 运行手册（历史兼容入口）

> 状态：已归档，不再作为当前架构事实来源。
> 更新：2026-08-23

该文件保留是为了兼容旧链接。此前版本把 Temporal 描述为办公任务的默认主运行时、
把 `AgentOrchestrator` 描述为单一控制中心；两项都已不符合当前代码。

请按主题阅读当前文档：

| 主题 | 当前事实来源 |
| --- | --- |
| 办公 DAG、默认运行时、等待状态、effect journal、逻辑计划 | [CURRENT_DAG_ARCHITECTURE.md](CURRENT_DAG_ARCHITECTURE.md) |
| 工具选择、Top-K、ReAct、审批门禁和结果回流 | [TOOL_SKILL_EXECUTION_GUIDE.md](TOOL_SKILL_EXECUTION_GUIDE.md) |
| Skill 生命周期、候选评测、MCP 准入 | [MCP_SKILL_GOVERNANCE.md](MCP_SKILL_GOVERNANCE.md) |
| 独立内核 workspace 与 app 适配边界 | [ORCHESTRATION_KERNEL_PACKAGE.md](ORCHESTRATION_KERNEL_PACKAGE.md) |
| 策略 YAML、特征契约、影子/生效模式 | [ROUTING_POLICY_MIGRATION.md](ROUTING_POLICY_MIGRATION.md) |
| Compose 部署、Alembic 与回归命令 | [ORCHESTRATION_DEPLOYMENT_GUIDE.md](ORCHESTRATION_DEPLOYMENT_GUIDE.md) |

当前默认是 `AGENT_ORCHESTRATION=legacy`：这里的 `legacy` 指持久化的自建 asyncio DAG，
不是旧的自由工具循环。Temporal 仅灰度承接可证明安全的静态只读任务；写操作、审批和
L3 动态子图仍由默认 DAG 运行时处理。
