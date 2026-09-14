"""多智能体协作编排（办公模式）.

三层架构：
  指挥层  Planner        → 意图拆解 → 任务树（DAG）
  执行层  WorkerAgent    → 领取任务，调用技能执行（React 多轮）
  工具层  技能插件        → 原子能力（web_search / query_knowledge / 本地文件等）

另含：质检钩子（Review）、任务状态机（TaskStatus）、DAG 编排器（execute_dag）。

## 包入口为什么是"轻"的

本文件只**急切导入公共模型**；``AgentOrchestrator`` / ``orchestrator`` 由 PEP 562 的
模块级 ``__getattr__`` 按需加载。动因是实测出来的，不是风格偏好：

* 应用里有大量模块只想引用一个模型或一个工具函数（``effects`` / ``timeout_ladder`` /
  ``plan_compiler`` / ``admission`` …），却因为父包 ``__init__`` 急切导入 orchestrator 单例，
  被迫一起加载 ``redis`` / ``sqlalchemy`` / ``langgraph`` / ``app.repositories``——
  实测**每个叶子模块的导入都是 ~13 秒**，且与它自己的依赖无关；
* 它同时构成循环的最后一环：``app.repositories → orchestration.models →
  orchestration.__init__ → orchestrator → app.repositories``。包入口一轻，这个环就断了。

## 两条导入约定（新代码请遵守）

```python
from app.agents.orchestration.models import Job            # ✅ 首选：直接指向真实模块
from app.agents.orchestration.orchestrator import orchestrator   # ✅ 单例的真实来源

from app.agents.orchestration import orchestrator          # ⚠️ 兼容用法，不推荐
```

最后一种写法与同名子模块 ``orchestrator.py`` **共享一个名字**，语义取决于"谁先被导入"：
``__getattr__`` 保证**首次访问**给出单例，但同一个进程里只要有人先导入过子模块，
包属性就是模块对象（Python 的标准行为）。新代码一律用前两种写法——
``tests/structure/test_orchestration_entry_light.py`` 会扫描仓库，禁止新增第三种。
"""

from app.agents.orchestration.models import Job, JobStatus, TaskNode, TaskStatus

__all__ = [
    "AgentOrchestrator",
    "orchestrator",
    "Job",
    "JobStatus",
    "TaskNode",
    "TaskStatus",
]

#: 按需加载的重符号：导入它们会拉起整个编排系统（见模块说明）。
_LAZY_ATTRS = frozenset({"AgentOrchestrator", "orchestrator"})


def __getattr__(name: str):
    """PEP 562：重符号按需加载，让包入口保持轻量。

    用 ``importlib.import_module`` 而不是 ``from ... import ...``：后者在包属性缺失时
    会再次触发本函数（同名子模块 + 同名导出），直接递归。

    **刻意不缓存**：把同名子模块的导出写回包属性会让
    ``import app.agents.orchestration.orchestrator as m`` 拿到实例而不是模块——
    那是在修一个歧义的同时造出另一个。标准导入语义优先。
    """
    if name not in _LAZY_ATTRS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(f"{__name__}.orchestrator")
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY_ATTRS})
