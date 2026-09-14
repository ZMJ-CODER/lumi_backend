"""编排根目录纪律（目录纪律棘轮）。

## 目标布局与"停止线"

```text
packages/orchestration        只放纯编排规则与稳定协议（不依赖 app）
app/agents/orchestration      应用编排适配，按 submission / planning / execution /
                              recovery / temporal / react 细分
app/agents/orchestration 根目录 只保留少量门面、公共模型与跨模块协调入口
```

**停止线**：不要再把应用适配层搬进 `lumi_orch`（Redis/PG/Temporal 客户端、Worker/Skill、
LLM 调用、办公逻辑、SSE、运行时配置、提交/恢复/审批适配都属于应用层）；
也不要让根目录继续长出新的业务大模块——新能力请进对应子包。

## 验收看四件事（不是"还剩多少文件"）

1. `lumi_orch` 不依赖 app → AST 门禁规则 1（阻塞）；
2. 应用层基础设施没有反向进入内核 → 门禁规则 7（阻塞）；
3. 根目录不再新增大型业务模块 → **本文件**（棘轮：名单只减不增、体积只降不升）；
4. 单个文件的职责与变更原因基本说得清 → 本文件要求根目录模块**都有模块 docstring**。

棘轮的意义：不要求一次拆完，但**不许倒退**。每完成一批拆分（react / recovery /
submission / step_run / temporal），就同时把下面的名单与预算改小。
"""

from __future__ import annotations

import ast

import pytest

from _paths import REPO_ROOT

ROOT_DIR = REPO_ROOT / "app" / "agents" / "orchestration"

#: 根目录允许存在的模块（**只减不增**）。新增模块要么进子包，要么在这里说明理由。
#:
#: P5 之后根目录只剩：包入口、公共模型、编排门面与两个"执行/重放"门面。
#: 其余 46 个模块已按职责迁入 admission / planning / preflight / runtime / execution /
#: office / submission / recovery / react / step / temporal 等子包。
_ROOT_MODULES_ALLOWED = frozenset(
    name.strip()
    for name in """
    __init__ models orchestrator react_runner step_run_service
    """.split()
)

#: 根目录**代码行数上限**（棘轮：只降不升）。当前 1407 行
#: （P5 把根目录 55 模块 / 11216 行收敛到 5 模块）。
_ROOT_LINE_BUDGET = 1407

#: 单文件行数上限；超过即必须进子包，或在这里登记并说明"为什么它必须留在根目录"。
_MODULE_LINE_CEILING = 400

#: 允许超过上限的根模块（**只减不增**）：它们都是后续拆分对象。
_BIG_MODULES_ALLOWED = {
    "orchestrator": "门面 + 跨模块协调器（恢复协调已抽到 recovery/coordination.py）",
    "step_run_service": "执行流程门面（状态适配/事件投影/持久化已抽到 step/ 子包）",
}


def _root_modules() -> dict[str, int]:
    """根目录模块 → 行数（不含 ``__pycache__``）。"""
    out: dict[str, int] = {}
    for path in sorted(ROOT_DIR.glob("*.py")):
        out[path.stem] = len(path.read_text(encoding="utf-8").splitlines())
    return out


def test_root_has_no_unregistered_module():
    """根目录不允许出现新模块（新能力请进 submission/planning/execution/recovery/temporal/react）。"""
    actual = set(_root_modules())
    unexpected = sorted(actual - _ROOT_MODULES_ALLOWED)
    assert unexpected == [], f"根目录新增了模块（请放进子包，或在此登记理由）：{unexpected}"
    missing = sorted(_ROOT_MODULES_ALLOWED - actual)
    assert missing == [], f"名单里的模块已不存在，请同步收紧名单：{missing}"


def test_root_line_budget_only_shrinks():
    """根目录总体积只降不升（棘轮）。"""
    total = sum(_root_modules().values())
    assert total <= _ROOT_LINE_BUDGET, (
        f"根目录总行数 {total} 超过预算 {_ROOT_LINE_BUDGET}；"
        "请把新增内容放进子包，或说明后收紧预算"
    )


@pytest.mark.parametrize("name", sorted(_BIG_MODULES_ALLOWED))
def test_big_modules_are_still_the_known_ones(name):
    """超过行数上限的根模块必须是**已登记的拆分对象**（拆完就从名单里删掉）。"""
    assert name in _ROOT_MODULES_ALLOWED, name
    assert _BIG_MODULES_ALLOWED[name], "必须写明为什么它还在根目录"
    assert (ROOT_DIR / f"{name}.py").exists(), f"{name} 已不存在，请从名单删除"


def test_no_unregistered_big_module_in_root():
    """超过行数上限的模块必须已登记——防止"顺手又写了一个 500 行文件"。"""
    offenders = sorted(
        name
        for name, lines in _root_modules().items()
        if lines > _MODULE_LINE_CEILING and name not in _BIG_MODULES_ALLOWED
    )
    assert offenders == [], f"这些根模块超过 {_MODULE_LINE_CEILING} 行且未登记：{offenders}"


def test_every_root_module_states_its_responsibility():
    """验收第 4 条：根目录每个模块都要有 docstring（一句话说清职责）。"""
    missing: list[str] = []
    for path in sorted(ROOT_DIR.glob("*.py")):
        if path.stem == "__init__":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - 仓库文件都应能解析
            missing.append(f"{path.stem}(语法错误)")
            continue
        doc = ast.get_docstring(tree) or ""
        if len(doc.strip()) < 10:
            missing.append(path.stem)
    assert missing == [], f"根目录模块缺少职责说明（docstring）：{missing}"


def test_split_subpackages_exist_and_root_copies_are_gone():
    """已完成的拆分：子包在、根目录的旧文件没了（防止"搬了但没删"）。"""
    subpackages = {
        "react", "recovery", "step", "submission", "temporal", "execution",
        "admission", "planning", "preflight", "runtime", "office",
    }
    for name in sorted(subpackages):
        assert (ROOT_DIR / name).is_dir(), f"缺少子包 {name}/"
        assert not (ROOT_DIR / f"{name}.py").exists(), f"{name}.py 与子包 {name}/ 不应同时存在"
    moved = {
        # P2/P5 迁走的模块必须全部不在根目录（防止"搬了又长回来"）
        "job_recovery_service",
        "failed_job_recovery_service",
        "failed_job_replan_service",
        "logical_plan_replan_service",
        "effect_journal_recovery",
        "replan_evidence_service",
        "replan_policy",
        "plan_compiler",
        "office_plan_selection_service",
        "capability_preflight",
        "capability_preflight_service",
        "state",
        "effects",
        "presentation",
        "context",
        "tca",
        "runtime_gateway",
    }
    assert moved.isdisjoint(_root_modules()), "这些模块应已迁入子包"
    assert not (ROOT_DIR / "react_runner.py").stat().st_size == 0
    # react_runner 是**同包门面**（不是跨包兼容壳）：只允许聚合本包内容
    source = (ROOT_DIR / "react_runner.py").read_text(encoding="utf-8")
    assert "app.agents.orchestration.react" in source
