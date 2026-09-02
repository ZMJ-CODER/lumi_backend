# 规划包边界

规划相关代码集中在 `app/agents/orchestration/planning`，按职责拆分为：

| 模块 | 职责 |
| --- | --- |
| `context.py` | 不可变请求上下文与旧 Planner 参数适配 |
| `contracts.py` | `Planner`、`TaskTree`、规划模型错误契约 |
| `normalizer.py` | 原子工具节点转换、Worker 兼容降级、串行化 |
| `prompting.py` | 规划提示词、注册表能力摘要、允许角色列表 |
| `template_params.py` | 模板默认参数的确定性抽取 |
| `strategies.py` | 模板和半结构模式规划策略 |
| `templates.py` | 高频办公流程模板（文档分析、发票筛选、对比/合并、翻译、早晚报） |
| `patterns.py` | ETL、条件路由等半结构 DAG 模式编译 |
| `static_routes.py` | 已知动作链的静态 DAG 编译 |
| `office_compound.py` | 文本生成与待办写入组合计划 |
| `read_only_dag.py` | 显式纯文本 A/B 并行分析 DAG |
| `compilation.py` | 提交前规范化、能力校验和一次反馈重规划 |
| `manifests.py` | 清单规划入口；游标语义复用 `lumi_orch.manifest` |

`planner.py` 仍是业务门面，负责办公文档、项目、LLM 和路由上下文的组合；
不拥有通用执行循环。规划模块只有本包这一条入口：根目录下曾存在的
`plan_context.py`、`route_plan.py`、`templates.py` 等转发文件已删除，新增代码
不得恢复同类兼容层。

清单来源授权、自然语言清洗和 Redis/Temporal 持久化仍属于应用适配层；
游标、进度、前沿选择、补图校验等与业务无关的逻辑继续由
`lumi-orchestration` 提供。

## 路由词典边界

`config/agent_policies/route_intent_patterns.yaml` 保存文件操作、RAG、外部
操作、多步连接、状态型推理和事实问题的词汇及最大匹配间隔。`routing_patterns.py`
不再维护这些业务词汇，只提供通用的窗口匹配器，并以兼容对象支持旧代码的
`.search()` 调用。新增同义词或调整词间距离只需修改并校验 YAML；否定词、
安全边界和特征优先级仍属于 Python 求值语义，避免把执行逻辑配置化。

## 无兼容层收紧顺序

1. **规划层（已完成）**：所有规划实现和导入统一使用 `planning.*`；删除根目录转发模块。
2. **执行层**：业务节点只实现 `NodeHandler` 和节点规格；可靠性、资源、效果日志和遥测只从
   `lumi_execution` 导入，不在业务层保留执行器别名。
3. **编排层**：API/业务协调器仅依赖 `RuntimeGateway` 与 `ExecutionBackend` 契约；具体
   Legacy/Temporal 后端只能在 `backends/` 注册，不允许在业务代码中按后端类型分支。
4. **删除门槛**：每次删除一个旧模块前，仓库内 `rg` 必须无生产导入；动态导入改为注册表中的
   显式键；对应模块测试改为新入口测试，而不是保留兼容性测试。
