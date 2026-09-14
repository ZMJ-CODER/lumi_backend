"""架构边界门禁的对抗性测试（证明门禁**真的**拦得住，而不是永远通过）。

与 ``tests/test_unsafe_call_gate.py`` 同一套路：每个规则都同时断言两件事——

1. **真违规判定为违规**（含容易被正则漏掉的形状：相对导入、别名、星号导入、字符串式
   动态导入、跨包 re-export 壳）；
2. **合法写法不误报**（自己包的聚合入口、公共 DTO、有自有行为的模块、非域间依赖），
   因为"零误报"是这套门禁能被团队接受的前提。

最后三组用例守的是**基线纪律**，它们让基线只能变短、白名单只能变短：

* 基线里的每一条都仍然是真实违规（修好了就必须 ``--update-baseline`` 让它消失）；
* 白名单里的每个文件都仍然存在、且仍然真的是兼容壳（删了壳就要删条目）；
* 阻塞规则（1、4）在当前仓库上零新增违规——这就是 CI 的判据。
"""

from __future__ import annotations

import importlib.util
import sys

from _paths import REPO_ROOT
REPO_ROOT = REPO_ROOT


def _gate():
    """导入门禁脚本（``tools/`` 是命名空间包，仍按文件加载，避免依赖 sys.path）。"""
    path = REPO_ROOT / "tools" / "check_architecture.py"
    spec = importlib.util.spec_from_file_location("_arch_gate", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["_arch_gate"] = module
    spec.loader.exec_module(module)
    return module


gate = _gate()

_CONTRACTS_FILE = "packages/contracts/src/lumi_contracts/thing.py"
_ORCH_FILE = "packages/orchestration/src/lumi_orch/thing.py"


def _scan(source: str, module: str, *, is_package: bool = False):
    return gate.scan_source(source, module=module, is_package=is_package)


def _details(violations, rule: str) -> list[str]:
    return [v.detail for v in violations if v.rule == rule]


# ── 1. 规则 1：packages/* 不得 import app.*（阻塞）────────────────


def test_packages_importing_app_is_flagged():
    found = _scan("from app.core.config import settings\n", gate.module_for_rel(_CONTRACTS_FILE))
    assert _details(found, "1") == ["lumi_contracts → app.core.config"], found

def test_packages_reaching_into_capabilities_is_flagged():
    found = _scan("from app.agents.capabilities import broker\n", gate.module_for_rel(_ORCH_FILE))
    assert _details(found, "1"), "编排内核反向依赖 app 侧能力实现必须被拦下"


def test_dynamic_import_cannot_bypass_rule_1():
    """字符串式导入是绕过静态检查最省事的办法，必须被识别。"""
    source = "import importlib\nimportlib.import_module('app.knowledge.retrieval.knowledge')\n"
    found = _scan(source, gate.module_for_rel(_ORCH_FILE))
    assert _details(found, "1"), "字符串式动态导入同样是 packages → app 的越界"


def test_plain_import_line_cannot_bypass_rule_1():
    found = _scan("import app.knowledge  # noqa\n", gate.module_for_rel(_ORCH_FILE))
    assert _details(found, "1")


def test_contracts_importing_own_submodule_is_not_a_violation():
    found = _scan("from lumi_contracts.plugins import manifest\n", gate.module_for_rel(_CONTRACTS_FILE))
    assert not _details(found, "1")


# ── 2. 规则 2：域间只走公共面（报告）──────────────────────────


def test_cross_domain_import_is_flagged():
    found = _scan("from app.knowledge.retrieval.knowledge import search\n", "app.office.docs")
    assert _details(found, "2") == ["office→knowledge: app.knowledge.retrieval.knowledge"], found


def test_relative_import_is_resolved_to_absolute_module():
    """``from ..knowledge.retrieval import knowledge``：正则扫绝对路径会漏，AST 不能漏。"""
    found = _scan("from ..knowledge.retrieval import knowledge\n", "app.office.docs")
    assert _details(found, "2") == ["office→knowledge: app.knowledge.retrieval"], found


def test_only_one_violation_per_dependency():
    """同一件事只报一次：模块 + 成员两条 ref 归并成一条（否则基线会虚高一倍）。"""
    found = _scan(
        "from app.knowledge.retrieval.knowledge import search\nfrom app.knowledge.retrieval.query_rewriter import get_retrieval_queries\n",
        "app.office.docs",
    )
    assert len(_details(found, "2")) == 1, found


def test_star_import_is_flagged():
    found = _scan("from app.knowledge import *\n", "app.memory.retrieval")
    assert _details(found, "2") == ["memory→knowledge: app.knowledge"], found


def test_public_dto_is_allowed_across_domains(monkeypatch):
    monkeypatch.setitem(gate.DOMAIN_PUBLIC, "knowledge", ("app.knowledge.contracts",))
    found = _scan("from app.knowledge.contracts import DocumentRef\n", "app.office.docs")
    assert not _details(found, "2"), "公开 DTO 属于方案允许的跨域直接依赖"


def test_same_domain_import_is_not_a_violation():
    found = _scan("from app.knowledge.embedding.embeddings import embed_texts\n", "app.knowledge.retrieval.knowledge")
    assert not _details(found, "2")


# ── 3. 规则 3：agents 不得 import 业务域实现（报告）────────────


def test_agents_importing_office_impl_is_flagged():
    found = _scan("from app.office.docs import generic_outputs_dir\n", "app.agents.roles.atomic")
    assert _details(found, "3"), "agents 直接 import 办公域实现必须被看见"


def test_agents_importing_workspace_is_not_rule_3():
    """工作区访问在 agents 里是工具/Port 的职责，规则 3 只管 knowledge/memory/office。"""
    found = _scan("from app.workspace.read.navigator import read_file\n", "app.agents.roles.atomic")
    assert not _details(found, "3")


# ── 4. 规则 4：禁止新增跨包 re-export 壳（阻塞）──────────────────


def test_cross_package_shim_is_detected():
    source = '"""薄适配层。"""\n\nfrom lumi_orch.execution_mode import *  # noqa: F401,F403\n'
    assert gate.detect_shim(source, module="app.agents.orchestration.adapters.execution_mode")


def test_named_reexport_shim_is_detected():
    source = (
        '"""壳。"""\n\nfrom __future__ import annotations\n\n'
        "from app.agents.resource_coordination import (\n"
        "    ResourceClaim,\n"
        "    resource_coordinator,\n"
        ")\n\n"
        '__all__ = ["ResourceClaim", "resource_coordinator"]\n'
    )
    assert gate.detect_shim(source, module="app.agents.orchestration.adapters.resources")


def test_package_aggregator_is_not_a_shim():
    """``__init__.py`` 聚合自己包的子模块是正常入口，不是兼容壳。"""
    source = "from .manager import PluginManager\nfrom .quota import quota_spec_for\n"
    assert gate.detect_shim(source, module="app.plugins", is_package=True) is None


def test_module_with_own_behavior_is_not_a_shim():
    source = (
        "from lumi_orch.run_view import run_view\n\n"
        "def build(job):\n"
        "    return run_view(job)\n"
    )
    assert gate.detect_shim(source, module="app.agents.orchestration.thing") is None


def test_import_side_effect_module_is_not_a_shim():
    """只为注册副作用而 import 的模块（没有跨包 from-import）不是壳。"""
    source = "import app.agents.roles  # noqa: F401\n"
    assert gate.detect_shim(source, module="app.agents.bootstrap_roles") is None


def test_whitelisted_shims_are_exempt_not_failures():
    """白名单是"豁免"，不是"基线"：命中的壳不判失败，但必须能在报告里看见。

    P7 之后白名单为空，所以这里同时断言"豁免数与白名单一致"——空名单就该零豁免。
    """
    report = gate.check(rules={"4"})
    assert not report.new
    assert all(v.path in gate.load_shims() for v in report.exempt)
    assert len(report.exempt) == len(gate.load_shims())


# ── 5. 规则 5 / 6：包分层与 core 洁净度（报告）────────────────


def test_lumi_orch_importing_execution_is_flagged():
    found = _scan("from lumi_execution.step_contract import StepContract\n", gate.module_for_rel(_ORCH_FILE))
    assert _details(found, "5") == ["lumi_orch → lumi_execution"], found


def test_lumi_execution_using_orch_dto_is_allowed():
    found = _scan("from lumi_orch.job_spec import JobSpec\n", "lumi_execution.policy")
    assert not _details(found, "5"), "job_spec/dag 是公共 DTO 与稳定纯函数，属于允许的直接依赖"


def test_lumi_execution_cannot_reach_orch_policy():
    """允许边上还有模块级收窄（与 packages/orchestration/tests/test_kernel_boundaries.py 一致）。"""
    found = _scan("from lumi_orch.execution_policy import POLICY_REACT\n", "lumi_execution.policy")
    assert _details(found, "5"), "执行内核不得依赖编排策略/状态/视图"
    assert "只允许" in _details(found, "5")[0]


def test_core_importing_domain_is_flagged():
    found = _scan("from app.knowledge.retrieval.knowledge import search\n", "app.core.helpers")
    assert _details(found, "6") == ["core→knowledge: app.knowledge.retrieval.knowledge"], found


def test_core_domain_word_in_filename_is_flagged():
    """规则 6 的**文件名**那一半：``app/core`` 里不许再出现 domain 词。

    用合成模块名而不是当前树里的真实文件：P4 把 ``core/rag_config.py`` 搬去
    ``app/knowledge/config.py`` 之后，核心区里已经没有带 domain 词的文件了——
    规则本身仍然必须生效（新加一个 ``core/memory_store.py`` 要立刻被拦下）。
    """
    found = _scan("x = 1\n", "app.core.memory_store")
    assert any("domain 词" in detail for detail in _details(found, "6")), found


def test_core_technical_module_is_clean():
    found = _scan("from app.core.redis import get_redis\n", "app.core.deps")
    assert not _details(found, "6")


# ── 6. 域公开面（P4 收尾）：跨域只准走 api，深 import 一律拦下 ──


def test_domain_api_surface_is_allowed_across_domains():
    """跨域依赖对方的 ``api`` 模块 = 合法（那是它公开承诺的契约）。"""
    found = _scan("from app.knowledge.api import search_user_knowledge\n", "app.office.docs")
    assert not _details(found, "2"), found
    found = _scan("from app.office.api import push_delta\n", "app.agents.orchestration.execution.node")
    assert not _details(found, "3"), found


def test_deep_import_into_a_domain_is_still_flagged():
    """域内实现不是公开面：同一个符号从深层模块取，必须被拦下。"""
    found = _scan("from app.knowledge.retrieval.knowledge import search_user_knowledge\n", "app.office.docs")
    assert _details(found, "2"), "深层实现 import 不该被 api 白名单顺带放行"
    found = _scan("from app.office.docs import resolve_generic_output\n", "app.agents.roles.direct_llm")
    assert _details(found, "3"), found


def test_declared_public_surfaces_exist_on_disk():
    """``DOMAIN_PUBLIC`` 里的每条都要能落到真实模块（防拼写漂移）。"""
    for domain, entries in gate.DOMAIN_PUBLIC.items():
        for entry in entries:
            rel = entry.replace(".", "/") + ".py"
            assert (REPO_ROOT / rel).exists(), f"{domain} 的公开面不存在：{entry}"


def test_reclassified_modules_are_not_domains():
    """``prompts``/``scene_manager``/``response_format`` 是跨场景设施，不属于任何业务域。

    它们曾经按目录被归进 office，导致 `chat_agent → office` 这类**假违规**。
    """
    flattened = {prefix for prefixes in gate.DOMAIN_PREFIXES.values() for prefix in prefixes}
    for module in ("app.services.prompts", "app.services.scene_manager", "app.services.response_format"):
        assert module not in flattened, f"{module} 不该被算作业务域"


def test_blocking_rule_set_covers_domain_isolation_now():
    """P4 收尾后，域隔离与 agents 边界都必须是**阻塞**规则。"""
    blocking = {rule.id for rule in gate.RULES if rule.mode == "block"}
    assert {"1", "2", "3", "4", "7"} <= blocking
    assert {rule.id for rule in gate.RULES if rule.mode == "report"} == {"5", "6"}


# ── 7. 基线纪律（只减不增）────────────────────────────────────


def test_baseline_entries_are_well_formed():
    baseline = gate.load_baseline()
    assert baseline, "基线文件必须存在（哪怕是空的规则说明）"
    for key in baseline:
        rule, _, rest = key.partition("|")
        assert rule in gate.RULE_BY_ID, f"基线里的规则号无效：{key}"
        assert "|" in rest, f"基线条目缺少细节字段：{key}"
        path = rest.split("|", 1)[0]
        assert path.endswith(".py"), f"基线条目缺少文件路径：{key}"


def test_baseline_has_no_stale_entries():
    """修好的违规必须从基线里消失——否则基线会变成"永久豁免清单"。"""
    report = gate.check()
    actual = {v.key for v in report.new} | {v.key for v in report.known}
    stale = gate.load_baseline() - actual
    assert not stale, (
        "基线里有已经不存在（或已修好）的条目，请运行 "
        "`python tools/check_architecture.py --update-baseline` 让它缩小：\n  "
        + "\n  ".join(sorted(stale)[:10])
    )


def test_compat_shim_whitelist_is_empty_after_p7():
    """P7 之后白名单**必须为空**：兼容壳一个不剩（"只减不增"的终点）。

    如果哪天又出现了条目，说明有人新建了一层转发壳——那正是规则 4 要拦的事，
    这里再钉一次：白名单要么空，要么里面的每条都必须是**真实存在的纯壳**。
    """
    shims = gate.load_shims()
    for rel, reason in shims.items():
        path = REPO_ROOT / rel
        assert path.exists(), f"白名单里的文件已不存在，请从 tools/compat_shims.txt 移除：{rel}"
        assert reason, f"白名单条目必须写明理由与删除计划：{rel}"
        source = path.read_text(encoding="utf-8-sig")
        assert gate.detect_shim(source, module=gate.module_for_rel(rel), is_package=path.name == "__init__.py"), (
            f"白名单里的文件已经不是纯壳（可能已经长出行为），请移除条目并让规则 4 正常判定：{rel}"
        )
    assert not shims, f"P7 之后不该再有兼容壳，实际还有：{sorted(shims)}"
    # 壳都删了，规则 4 的豁免数必须是 0（否则说明白名单没生效/还有漏网的）
    report = gate.check(rules={"4"})
    assert not report.exempt


def test_tree_has_no_new_violations_for_blocking_rules():
    """CI 判据：所有阻塞规则（1/2/3/4/7）零新增违规。"""
    blocking = tuple(sorted(rule.id for rule in gate.RULES if rule.mode == "block"))
    report = gate.check(block=set(blocking))
    assert not report.new, "阻塞规则出现新增违规：\n  " + "\n  ".join(
        f"[{v.rule}] {v.path}: {v.detail}" for v in report.new
    )
    assert report.blocking_rules == blocking
    assert report.scanned_files > 500, "扫描范围异常（应覆盖 app/packages/tests/scripts…）"


def test_report_rules_do_not_fail_the_gate_but_stay_visible():
    """报告模式的意义：违规进来时门禁不红，但**必须**能在报告里看见。"""
    report = gate.check(block=set())
    assert not report.failed
    assert report.new or report.known
