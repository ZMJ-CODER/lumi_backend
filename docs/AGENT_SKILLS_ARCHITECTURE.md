# 智能体技能与沙箱架构（预留）

> 状态：架构预留，默认全部关闭。现有聊天链路（上下文 → 记忆 → RAG → LLM → 回复）
> 不受任何影响。以下分层为后期"技能调用"设计的扩展点。

## 分层设计（解耦核心）

```
┌─────────────────────────────────────────────────────┐
│ 智能体层（LLM 决策）                                  │
│ 决定"调用哪个技能、传什么参数"                          │
└───────────────────────┬─────────────────────────────┘
                        │ 结构化技能请求
┌───────────────────────▼─────────────────────────────┐
│ 技能层（Skill）                                      │
│ 声明能力契约：name / description / parameters_schema  │
│ execute() 实现业务语义                                │
└───────────────────────┬─────────────────────────────┘
                        │ 需要执行代码/命令时
┌───────────────────────▼─────────────────────────────┐
│ 沙箱层（Sandbox）                                    │
│ 隔离执行环境：run_script / run_command               │
│ 只负责"在哪里执行、如何隔离"，不关心业务语义             │
└─────────────────────────────────────────────────────┘
```

职责边界：
- 智能体层**不直接执行**任何能力，只输出技能调用意图
- 技能层**不关心**执行环境（本地/容器/WASM）
- 沙箱层**不关心**业务，只保证隔离、限时、限资源

## 代码位置

| 层 | 模块 | 说明 |
|---|---|---|
| 技能 | `app/agents/skills/base.py` | `Skill` 抽象 + `SkillResult` |
| 技能注册 | `app/agents/skills/registry.py` | `SkillRegistry` 单例 |
| 沙箱抽象 | `app/agents/sandbox/base.py` | `Sandbox` 抽象 + `SandboxResult` |
| 沙箱注册 | `app/agents/sandbox/registry.py` | `get_sandbox(type)` 懒加载单例 |
| 本地沙箱 | `app/agents/sandbox/local.py` | 占位实现，未启用时一律拒绝 |
| 调用循环 | `app/agents/loop.py` | `maybe_run_skills()` 预留入口 |
| 编排器挂点 | `app/services/orchestrator.py` | LLM 回复后调用 `maybe_run_skills` |

## 配置

```ini
AGENT_SKILLS_ENABLED=false            # 技能调用开关
AGENT_SANDBOX_TYPE=local              # 沙箱类型：local / docker / wasm（预留）
AGENT_SANDBOX_TIMEOUT_SECONDS=30
AGENT_SANDBOX_MAX_OUTPUT_CHARS=8000
```

## 后期扩展步骤

**新增一个技能：**
1. 继承 `Skill`，实现 `name / description / parameters_schema / execute`
2. `SkillRegistry.register(skill)` 注册
3. 需要执行代码/命令的技能，在 `execute` 里通过 `get_sandbox()` 获取沙箱实例执行

**新增一种沙箱：**
1. 继承 `Sandbox`，实现 `run_script / run_command`
2. `register_sandbox("docker", DockerSandbox)` 注册
3. 配置 `AGENT_SANDBOX_TYPE=docker` 切换

**启用完整技能调用循环（二期）：**
在 `app/agents/loop.py` 的 `maybe_run_skills` 中实现：
LLM function calling → 技能校验 → 沙箱执行 → 结果回填 → 循环至最终答复。
编排器挂点已就位，无需改动主流程。

## 安全边界（二期实现时）

- 沙箱必须实施：超时强制终止、输出截断（`AGENT_SANDBOX_MAX_OUTPUT_CHARS`）、
  资源限制（CPU/内存/网络）、禁止访问宿主敏感路径
- 技能参数必须按 `parameters_schema` 校验，防止注入
- 默认拒绝所有未显式开启的执行（当前 local 沙箱即为此行为）
