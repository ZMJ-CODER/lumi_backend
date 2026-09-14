"""编排层的**薄适配壳**集中地。

这里只放"把内核包的名字换个路径再卖一遍"的模块，**不许长业务逻辑**：

======================  ==========================================
模块                    真实实现
======================  ==========================================
``execution_mode``      ``lumi_orch.execution_mode``
``execution_policy``    ``lumi_orch.execution_policy``
``job_run_view``        ``lumi_orch.run_view``
``step_resume``         ``lumi_execution.step_resume``
``step_sequence``       ``lumi_orch.step_sequence``
======================  ==========================================

两个固定规矩：

1. **新代码直接 import 内核包**（``from lumi_orch.run_view import run_view``），
   不要再往这里加文件；规则 4 会拦下新增的跨包 re-export 壳
   （``python tools/check_architecture.py``）。
2. 这批壳**当前零引用**（调用点早已直接指向内核包），已在
   ``tools/compat_shims.txt`` 登记，P7 统一删除。

**资源租约适配器不在这里**：``app.agents.resource_coordination`` 必须留在编排包之外，
因为 import ``app.agents.orchestration`` 会连带加载 Planner/LangGraph
（实测 26.5s vs 1.1s，见该模块 docstring），而原子工具执行路径不该付这个代价。
"""
