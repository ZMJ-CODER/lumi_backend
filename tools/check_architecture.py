#!/usr/bin/env python
"""静态扫描：分层与边界规则（AST 实现），配套《Lumi 后端结构重构方案 v2》§一/§六。

## 为什么必须是 AST

边界规则全都长在 ``import`` 上，而 ``import`` 的写法有五种形状（``import a.b``、
``from a import b``、``from . import b``、``import a.b as c``、字符串式
``importlib.import_module("a.b")``）。正则扫 ``app\\.services\\.rag`` 会漏掉相对导入
与别名，还会把文档字符串/注释里的路径算成依赖——门禁一旦误报就会被绕过或被关掉。
``ast`` 直接看节点形状，并且能**把相对导入还原成绝对模块名**（这是本门禁的核心能力：
没有它，``from ..rag import knowledge`` 这类写法完全隐形）。

## 规则表（模式是这一版的全部意义）

============================  ======  ==================================================
id 名称                        模式    含义
============================  ======  ==================================================
1  packages_no_app            阻塞    ``packages/*`` 不得 import ``app.*``（规则 1）
2  domain_isolation           报告    业务域之间只走 Port / 公共 DTO / 只读模型 / 领域错误
3  agents_no_domain_impl      报告    ``app/agents/**`` 不得 import 业务域实现
4  no_new_shim                阻塞    禁止新增 ≤15 行 re-export 兼容壳（白名单制）
5  package_layering           报告    包与包之间的允许依赖边（P3 后转阻塞）
6  core_no_domain             报告    ``app/core`` 不得出现 domain 词（P5 后转阻塞）
============================  ======  ==================================================

**先报告、后阻塞**：报告模式的违规只打印、不判失败，等基线稳定、例外写清楚之后再
在 :data:`RULES` 里把 ``mode`` 改成 ``"block"``——一次只收紧一条规则。

## 基线机制（存量违规不要求一次性清零）

``tools/architecture_baseline.txt`` 记录**当前已经存在**的违规（``规则|文件|细节``）。
判定顺序：

1. 命中 ``tools/compat_shims.txt`` 白名单的兼容壳 → **豁免**（白名单只减不增）；
2. 命中基线 → **存量**（打印，但不判失败）；
3. 其余 → **新增**：阻塞规则的任何新增违规都让门禁失败。

``--update-baseline`` 把当前违规全量写入基线（重构阶段推进时**缩小**它，不是刷新它）。

## 例外与豁免（写清楚，免得靠"大家都懂"）

| 情形 | 处理 | 为什么 |
| --- | --- | --- |
| 字符串式动态导入 | **照样扫**（`importlib.import_module("app.x")` / `__import__("app.x")`） | 不扫等于留一条一行就能绕过的后门 |
| 兼容壳（跨包 re-export） | 规则 4 走 `tools/compat_shims.txt` 白名单 | 它们是**有意保留**的迁移设施，不是违规；但只减不增 |
| 测试代码 | 与产品代码同一套规则 | 测试里的越界会变成"测试把错误架构固化下来" |
| 一次性迁移脚本（`scripts/migrations/`） | 不受域间规则约束（它们不属于任何业务域） | 任务是一次性的、跑完即弃，不该为它们设计依赖方向 |
| 存量违规 | 基线内不阻塞、不隐藏（默认打印） | "不要求一次性清零" ≠ "假装不存在" |

## 用法

    python tools/check_architecture.py                  # 全仓库，按规则表判定
    python tools/check_architecture.py app packages      # 只扫指定根
    python tools/check_architecture.py --rules 1,4       # 只跑指定规则
    python tools/check_architecture.py --block 2,3       # 临时把规则 2/3 当阻塞跑（试运行）
    python tools/check_architecture.py --update-baseline # 重写基线
    python tools/check_architecture.py --json            # 机器可读
    python tools/check_architecture.py --list-rules
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_TOOLS_DIR = Path(__file__).resolve().parent
BASELINE_PATH = _TOOLS_DIR / "architecture_baseline.txt"
SHIMS_PATH = _TOOLS_DIR / "compat_shims.txt"

#: 默认扫描根（与 ``scripts/check_unsafe_calls.py`` 保持同一视野）。
DEFAULT_ROOTS: tuple[str, ...] = ("app", "packages", "scripts", "celery_app", "tools", "tests", "plugins")

#: 跳过目录（构建产物、虚拟环境、各类本地缓存）。
SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".venv", "venv", "node_modules", "__pycache__", ".git", ".ptmp", ".pytest_cache",
        ".ruff_cache", ".ruff-uv-cache", ".ruff-uv-tools", ".uv-cache", ".uv-cache-codex",
        ".uv-cache-locust", ".uv-cache-test", ".pytest-tmp", ".pytest-workdir", ".ws-acceptance",
        "dist", "build", "artifacts", "logs", "data", "office_sandbox",
    }
)

#: 仓库内的包（``packages/*/src/<pkg>``）。``lumi_orchestration`` 是历史 egg-info 名的兼容别名。
REPO_PACKAGES: tuple[str, ...] = (
    "lumi_contracts",
    "lumi_orch",
    "lumi_execution",
    "lumi_capability",
    "lumi_skills",
)


@dataclass(frozen=True, slots=True)
class Rule:
    """一条边界规则。"""

    id: str
    name: str
    mode: str  # "block" | "report"
    summary: str


#: 规则表。收紧顺序写在《结构重构方案》§六：P0 只阻塞 1、4。
RULES: tuple[Rule, ...] = (
    Rule("1", "packages_no_app", "block", "packages/* 不得 import app.*"),
    # P4 收尾：四域迁完 + 各域有了 api 公开面之后，域间隔离转为**阻塞**。
    # 现在起："这个域能不能依赖那个域"由 DOMAIN_PUBLIC 决定，而不是靠人盯。
    Rule("2", "domain_isolation", "block", "业务域之间只走对方 api / 公共 DTO / Port"),
    Rule("3", "agents_no_domain_impl", "block", "app/agents/** 不得 import 业务域实现（只能用 api/Port）"),
    Rule("4", "no_new_shim", "block", "禁止新增 ≤15 行 re-export 兼容壳（白名单制）"),
    Rule("5", "package_layering", "report", "包之间只允许既定依赖方向"),
    Rule("6", "core_no_domain", "report", "app/core 不得出现 domain 词"),
    Rule("7", "pure_package_no_runtime", "block", "纯决策包不得 import 运行时设施（Redis/DB/FastAPI/日志/网络/内核）"),
)

RULE_BY_ID: dict[str, Rule] = {rule.id: rule for rule in RULES}

#: 兼容壳的判定阈值：**代码行**（不含文档字符串/注释/空行；import 的括号续行算在 import 上）。
#: ``SHIM_MAX_LINES`` 是方案里写的 15 行（第一档）；``SHIM_MAX_VERBOSE_LINES`` 是兜底上限
#: （"同样什么都没做，只是 import 换行写得很长"）。真正的判据是"零自有行为 + 跨包 re-export"。
SHIM_MAX_LINES = 15
SHIM_MAX_VERBOSE_LINES = 40

#: 业务域 → 模块前缀。域迁移时把**新位置**加进来；旧位置在兼容层删除时移除
#: （两套位置同时存在期间，规则照旧能看见跨域依赖）。
DOMAIN_PREFIXES: dict[str, tuple[str, ...]] = {
    "knowledge": (
        # P4 域 1 已迁完：知识域的家在 app/knowledge（解析 / 嵌入 / 检索 / 代码索引）。
        # 迁移期兼容层 app/services/rag/__init__.py 已在 P7 删除，不再有第二处位置。
        "app.knowledge",
    ),
    "workspace": (
        # P4 域 2 已迁完：工作区域的家在 app/workspace（read/ 与 write/ 分侧）
        "app.workspace",
    ),
    "memory": (
        # P4 域 3 已迁完：记忆域的家在 app/memory（对话 / 裁剪 / 任务召回 / 长期记忆 / 仓储 Port）
        "app.memory",
    ),
    "office": (
        # P4 域 4 已迁完：办公域的家在 app/office（文档编辑 / 上下文 / 技能工具 / 流 / 渲染）
        "app.office",
        # 注：``prompts`` / ``scene_manager`` / ``response_format`` **不再算作办公域**。
        # 它们是跨角色、跨场景的提示词与展示设施（chat_agent / react_runner / 聊天场景都用），
        # 归在 office 是当时的目录分组而不是依赖事实——把它们留在 office 名下会让规则 3
        # 报出"agents → office"的假违规，掩盖真正的耦合。归属在 P5 定（大概率 app/platform）。
    ),
}

#: 允许**跨域直接依赖**的模块前缀（公共 DTO / 只读查询模型 / 领域错误 / 稳定纯函数 / **Port**）。
#:
#: P4 收尾后的规矩很简单：**跨域只准 import 对方的 ``api`` 模块**（外加登记过的 Port）。
#: 每个域的 ``api.py`` 是一处可审计的公开清单——新增跨域依赖时，作者必须先把名字加进
#: 对方的 api（也就是先把"这是公开契约"这件事想清楚），而不是随手 import 深层实现。
DOMAIN_PUBLIC: dict[str, tuple[str, ...]] = {
    "knowledge": ("app.knowledge.api",),
    "workspace": (),
    # 记忆仓储是 Memory 域的 **Port**：编排层需要它来落记忆，但只能通过这个稳定接口。
    "memory": ("app.memory.repository",),
    "office": ("app.office.api",),
}

#: 规则 3 覆盖的域：``app/agents/**`` 不得直接 import 这三域的实现
#: （工作区访问在 agents 里是工具/Port 的职责，不在此列）。
AGENTS_FORBIDDEN_DOMAINS: tuple[str, ...] = ("knowledge", "memory", "office")

#: 包 → 允许依赖的其它仓库包（规则 5）。P0 按**实测现状**写下，P3 收紧
#: （``lumi_capability`` 的允许集合只有 ``lumi_contracts``——这是方案 §一 的硬规则）。
#:
#: P0 实测发现一个**真问题**（已登记进基线，规则 5 转阻塞前必须裁决）：
#: ``lumi_orch`` 与 ``lumi_execution`` **互相依赖**——``lumi_execution`` → ``lumi_orch``
#: 只用 ``job_spec`` / ``dag``（公共 DTO + 稳定纯函数，属于方案允许的直接依赖）；
#: 反向 ``lumi_orch.execution_mode`` → ``lumi_execution.step_contract`` 是**共享词汇表**。
#: 结论：环出现在"词汇表/DTO"层，裁决方向应是把共享词汇下沉到 ``lumi_contracts``，
#: 而不是把这条边加进允许集合（加进去等于把环藏起来）。
PACKAGE_LAYERS: dict[str, frozenset[str]] = {
    "lumi_contracts": frozenset(),
    "lumi_execution": frozenset({"lumi_contracts", "lumi_orch"}),
    "lumi_orch": frozenset({"lumi_contracts"}),
    "lumi_capability": frozenset({"lumi_contracts"}),
    "lumi_skills": frozenset({"lumi_contracts", "lumi_capability"}),
}

#: 允许边上的**模块级收窄**：``(来源包, 目标包) -> 允许的目标模块前缀``。
#: 没有条目的允许边不设前缀限制（整个目标包都允许）。
#:
#: ``lumi_execution → lumi_orch`` 与 ``packages/orchestration/tests/test_kernel_boundaries.py``
#: 里既有的断言保持一致：只允许共享规格 ``job_spec`` / ``dag``，
#: 不得碰编排策略/状态/视图（``execution_policy`` / ``state_machine`` / ``run_view``…）。
PACKAGE_EDGE_ALLOWANCES: dict[tuple[str, str], tuple[str, ...]] = {
    ("lumi_execution", "lumi_orch"): ("lumi_orch.job_spec", "lumi_orch.dag"),
}

#: 规则 7 覆盖的包：只抽"纯决策"的那几个。
#: 它们一旦 import 运行时设施（Redis/DB/FastAPI/日志/HTTP/编排内核），就说明抽错了东西——
#: "能被第二个服务复用"正是抽包的全部意义，混进 IO 之后这个意义立刻消失。
PURE_PACKAGES: dict[str, str] = {
    "lumi_capability": "能力域纯决策内核（P3：协议 / 档位 / 部署判定 / 候选择优）",
}

#: 纯包里禁止出现的模块前缀（含标准库与第三方运行时设施）。
PURE_FORBIDDEN_MODULES: tuple[tuple[str, str], ...] = (
    ("redis", "Redis 客户端属于运行时适配，留在 app"),
    ("fastapi", "FastAPI 视图不属于纯决策"),
    ("starlette", "ASGI 运行时不属于纯决策"),
    ("sqlalchemy", "数据库持久化不属于纯决策"),
    ("alembic", "迁移不属于纯决策"),
    ("celery", "任务队列不属于纯决策"),
    ("temporalio", "工作流运行时不属于纯决策"),
    ("langchain", "LLM 编排不属于纯决策"),
    ("httpx", "网络访问不属于纯决策"),
    ("requests", "网络访问不属于纯决策"),
    ("loguru", "日志是宿主应用的关注点；需要打点就通过回调暴露"),
    ("app", "纯包不得依赖应用"),
    ("lumi_orch", "纯包不得依赖编排内核"),
    ("lumi_execution", "纯包不得依赖执行内核"),
)

#: 规则 6 的 domain 词（``app/core`` 里出现即报告）。
CORE_DOMAIN_WORDS: tuple[str, ...] = (
    "job_", "memory_", "workspace_", "office_", "knowledge_", "rag_", "conversation_",
    "scene_", "document_", "artifact_", "plugin_",
)

#: 动态导入的成员名（``importlib.import_module("app.x")`` / ``__import__("app.x")``）。
_DYNAMIC_IMPORT_MEMBERS = frozenset({"import_module", "__import__"})

_BOM = "\ufeff"


@dataclass(frozen=True, slots=True)
class Violation:
    """一条边界违规（**不带行号**：行号会随无关改动漂移，基线的身份必须是路径+细节）。"""

    rule: str
    path: str
    detail: str

    @property
    def key(self) -> str:
        return f"{self.rule}|{self.path}|{self.detail}"


@dataclass(frozen=True, slots=True)
class ImportRef:
    """一条 import（相对导入已还原为绝对模块名）。"""

    module: str
    lineno: int
    dynamic: bool = False


# ── 路径 ↔ 模块名 ───────────────────────────────────────────


def _module_for(path: Path) -> str:
    """把文件路径还原成点号模块名；不在仓库内时退回文件名。"""
    try:
        rel = path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.name
    return module_for_rel(rel)


def module_for_rel(rel: str) -> str:
    """``app/agents/x.py`` → ``app.agents.x``；``packages/*/src/<pkg>`` 去前缀。"""
    posix = rel.replace("\\", "/")
    if posix.startswith("packages/") and "/src/" in posix:
        posix = posix.split("/src/", 1)[1]
    parts = [part for part in posix.split("/") if part]
    if not parts:
        return ""
    if parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_package(rel: str) -> bool:
    return rel.replace("\\", "/").endswith("/__init__.py") or rel.replace("\\", "/") == "__init__.py"


def _local_of_module(module: str, *, is_package: bool) -> str:
    """模块自己所属的包（用于"跨包桥接"判定）。"""
    if is_package:
        return module
    return module.rsplit(".", 1)[0] if "." in module else ""


def resolve_relative(module: str, *, is_package: bool, level: int, target: str | None) -> str:
    """把相对导入还原成绝对模块名（``level`` 是 ``ImportFrom.level``）。"""
    parts = [part for part in module.split(".") if part]
    if not is_package:
        parts = parts[:-1]
    if level > 1:
        parts = parts[: max(0, len(parts) - (level - 1))]
    base = ".".join(parts)
    if target:
        return f"{base}.{target}" if base else target
    return base


# ── AST 解析 ────────────────────────────────────────────────


def _iter_imports(tree: ast.AST, module: str, *, is_package: bool) -> list[ImportRef]:
    found: list[ImportRef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append(ImportRef(module=alias.name, lineno=node.lineno))
        elif isinstance(node, ast.ImportFrom):
            target = node.module
            if node.level:
                resolved = resolve_relative(module, is_package=is_package, level=node.level, target=target)
            else:
                resolved = target or ""
            for alias in node.names:
                # ``from a import b``：既可能是模块 ``a.b``，也可能是成员 ``a.b``。
                # 两条都记，由规则按前缀判定（模块判定看前缀，成员判定靠前缀不等）。
                found.append(ImportRef(module=resolved, lineno=node.lineno))
                if alias.name != "*":
                    found.append(ImportRef(module=f"{resolved}.{alias.name}" if resolved else alias.name, lineno=node.lineno))
        elif isinstance(node, ast.Call):
            member = None
            if isinstance(node.func, ast.Attribute):
                member = node.func.attr
            elif isinstance(node.func, ast.Name):
                member = node.func.id
            if member in _DYNAMIC_IMPORT_MEMBERS and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.append(ImportRef(module=first.value, lineno=node.lineno, dynamic=True))
    return found


def parse_module(source: str, *, module: str) -> tuple[ast.AST | None, bool]:
    text = source.lstrip(_BOM)
    try:
        return ast.parse(text, filename=module or "<string>"), True
    except SyntaxError:
        return None, False


def imports_of(source: str, *, module: str, is_package: bool = False) -> list[ImportRef]:
    """解析一段源码的 import（供测试与 CI 共用同一实现）。"""
    tree, ok = parse_module(source, module=module, )
    if not ok or tree is None:
        return []
    return _iter_imports(tree, module, is_package=is_package)


def detect_shim(source: str, *, module: str, is_package: bool = False) -> str | None:
    """判断一段源码是不是**re-export 兼容壳**；是则返回理由，否则 ``None``。

    判据（四条同时成立）：

    1. **只有搬运**：顶层语句只允许文档字符串、``import``/``from ... import``、
       ``__all__`` 赋值——**没有任何自有行为**；
    2. **确实在搬运**：至少有一条非 ``__future__`` 的 ``from ... import``；
    3. **是跨包桥接**：至少有一个来源模块**在本模块所属包之外**；
    4. **薄**：总代码行（不含文档字符串/注释/空行）≤ :data:`SHIM_MAX_LINES`
       或 ≤ :data:`SHIM_MAX_VERBOSE_LINES`。

    第 3 条是"零误报"的关键：``__init__.py`` 聚合自己子模块（``from .a import A``）是
    正常入口，不是兼容壳；只有把**别的包**的东西换个路径再卖一遍才算壳。

    第 1 条比"≤15 行"更准：真正的判据是"它自己什么也没做"，行数只是代理指标。
    ``app/contracts/plugins/manifest.py`` 是 39 行的同一种壳（37 行都是 import 的括号
    续行），按行数会漏判，按"零自有行为"必中。行数上限保留为兜底（防止把巨型聚合模块
    误判成壳），方案里的 15 行仍然作为第一档。
    """
    tree, ok = parse_module(source, module=module)
    if not ok or tree is None:
        return None
    body = list(getattr(tree, "body", []))
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]  # 模块文档字符串

    sources: list[str] = []
    code_lines = 0
    for node in body:
        span = (node.end_lineno or node.lineno) - node.lineno + 1
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            code_lines += span
            if isinstance(node, ast.ImportFrom) and node.module != "__future__" and node.level == 0 and node.module:
                sources.append(node.module)
            elif isinstance(node, ast.ImportFrom) and node.level:
                resolved = resolve_relative(module, is_package=is_package, level=node.level, target=node.module)
                if resolved and resolved != "__future__":
                    sources.append(resolved)
        elif isinstance(node, ast.Assign):
            code_lines += span
            if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
                return None  # 给别的名字赋值 = 有自有行为，不是纯壳
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "__all__":
            code_lines += span
        else:
            return None  # 函数/类/条件/循环/try… 都不是纯搬运

    if not sources or code_lines > SHIM_MAX_VERBOSE_LINES:
        return None
    own_package = _local_of_module(module, is_package=is_package)
    if own_package and all(src == own_package or src.startswith(own_package + ".") for src in sources):
        return None  # 只聚合自己包内的子模块 → 正常入口
    return "re-export " + ", ".join(sorted(set(sources)))


# ── 规则判定 ────────────────────────────────────────────────


def _domain_of(module: str) -> str | None:
    for name, prefixes in DOMAIN_PREFIXES.items():
        for prefix in prefixes:
            if module == prefix or module.startswith(prefix + "."):
                return name
    return None


def _is_domain_public(domain: str, module: str) -> bool:
    return any(module == prefix or module.startswith(prefix + ".") for prefix in DOMAIN_PUBLIC.get(domain, ()))


def _package_of(module: str) -> str | None:
    for pkg in REPO_PACKAGES:
        if module == pkg or module.startswith(pkg + "."):
            return pkg
    return None


_EXISTING_MODULES: set[str] | None = None


def _existing_modules() -> set[str]:
    """仓库里**真实存在**的模块名集合（懒加载一次，供目标选择用）。"""
    global _EXISTING_MODULES
    if _EXISTING_MODULES is None:
        found: set[str] = set()
        for path in iter_python_files(list(DEFAULT_ROOTS)):
            try:
                rel = path.resolve().relative_to(REPO_ROOT).as_posix()
            except ValueError:
                continue
            found.add(module_for_rel(rel))
            if path.name == "__init__.py":
                parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
                if parent:
                    found.add(module_for_rel(parent + "/__init__.py"))
        _EXISTING_MODULES = found
    return _EXISTING_MODULES


def _prefer_module(candidate: str, current: str) -> bool:
    """候选是否比当前更好：**真实存在的模块优先**，同类里短的优先。"""
    existing = _existing_modules()
    candidate_real = candidate in existing
    current_real = current in existing
    if candidate_real != current_real:
        return candidate_real
    return (len(candidate), candidate) < (len(current), current)


def scan_source(source: str, *, module: str, is_package: bool = False) -> list[Violation]:
    """扫一段源码，返回违规（不含基线/白名单判定）。

    **一条依赖只报一次**：``from app.knowledge.retrieval.knowledge import search`` 在 AST 里是
    两条 ref（模块 ``app.knowledge.retrieval.knowledge`` 与成员 ``…knowledge.search``），按 ref
    逐条报会把同一件事写两遍、让基线虚高一倍。这里按 ``(规则, 目标域/包)`` 归并，保留
    **最短**的模块路径——门禁要陈述的边界事实是"这个文件依赖了那个域"，成员清单不是它
    的职责（要看细节去读那一行 import）。

    选择目标时**优先取真实存在的模块**（磁盘上真有那个 .py 或包目录），其次才比长度：
    ``from app.office import docs`` 会同时产生 ``app.office`` 与 ``app.office.docs`` 两条 ref，
    取"存在的那个"既能给出更精确的细节，也让基线不会因为换一种 import 写法就漂移。
    """
    rel = module.replace(".", "/") + (".py" if not is_package else "/__init__.py")
    tree, ok = parse_module(source, module=module)
    if not ok or tree is None:
        return []
    refs = _iter_imports(tree, module, is_package=is_package)
    out: set[Violation] = set()
    own_domain = _domain_of(module)
    own_package = _package_of(module)
    hits: dict[tuple[str, str], str] = {}

    def _hit(rule: str, kind: str, target: str) -> None:
        current = hits.get((rule, kind))
        if current is None or _prefer_module(target, current):
            hits[(rule, kind)] = target

    for ref in refs:
        target = ref.module
        if not target:
            continue

        # 规则 1：packages/* 不得 import app.*
        if own_package and (target == "app" or target.startswith("app.")):
            _hit("1", "app", target)

        target_domain = _domain_of(target)
        public = bool(target_domain) and _is_domain_public(target_domain, target)

        # 规则 2：域间只走公共面
        if own_domain and target_domain and target_domain != own_domain and not public:
            _hit("2", target_domain, target)

        # 规则 3：app/agents/** 不得 import 业务域实现
        if module.startswith("app.agents.") and target_domain in AGENTS_FORBIDDEN_DOMAINS and not public:
            _hit("3", str(target_domain), target)

        # 规则 5：包之间只允许既定依赖方向（允许边上还可再收窄到具体模块）
        target_package = _package_of(target)
        if own_package and target_package and target_package != own_package:
            if target_package not in PACKAGE_LAYERS.get(own_package, frozenset()):
                _hit("5", target_package, target)
            else:
                narrowed = PACKAGE_EDGE_ALLOWANCES.get((own_package, target_package))
                if narrowed is not None and not any(
                    target == prefix or target.startswith(prefix + ".") for prefix in narrowed
                ):
                    _hit("5", f"{target_package}!{'/'.join(narrowed)}", target)

        # 规则 6：app/core 不得依赖业务域
        if module.startswith("app.core") and target_domain:
            _hit("6", target_domain, target)

    for (rule, kind), target in hits.items():
        if rule == "1":
            out.add(Violation("1", rel, f"{own_package} → {target}"))
        elif rule == "2":
            out.add(Violation("2", rel, f"{own_domain}→{kind}: {target}"))
        elif rule == "3":
            out.add(Violation("3", rel, f"agents→{kind}: {target}"))
        elif rule == "5":
            if "!" in kind:
                package, prefixes = kind.split("!", 1)
                out.add(Violation("5", rel, f"{own_package} → {target}（{package} 只允许 {prefixes}）"))
            else:
                out.add(Violation("5", rel, f"{own_package} → {kind}"))
        elif rule == "6":
            out.add(Violation("6", rel, f"core→{kind}: {target}"))

    # 规则 4：跨包 re-export 壳
    reason = detect_shim(source, module=module, is_package=is_package)
    if reason:
        out.add(Violation("4", rel, reason))

    # 规则 6（文件名）：app/core 的 domain 词
    if module.startswith("app.core"):
        name = module.rsplit(".", 1)[-1]
        for word in CORE_DOMAIN_WORDS:
            if name.startswith(word) or name == word.rstrip("_"):
                out.add(Violation("6", rel, f"core 模块名含 domain 词：{word}"))
                break

    # 规则 7：纯决策包不得出现运行时设施（Redis/DB/FastAPI/日志/网络/内核）
    for pure_package in PURE_PACKAGES:
        if not (module == pure_package or module.startswith(pure_package + ".")):
            continue
        for ref in refs:
            target = ref.module
            if not target:
                continue
            head = target.split(".", 1)[0]
            for forbidden, why in PURE_FORBIDDEN_MODULES:
                if target == forbidden or head == forbidden:
                    note = "（动态导入）" if ref.dynamic else ""
                    out.add(Violation("7", rel, f"{pure_package} 不得 import {target}{note}：{why}"))
                    break

    return sorted(out, key=lambda v: (v.rule, v.path, v.detail))


def scan_file(path: Path) -> list[Violation]:
    rel = ""
    try:
        rel = path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        rel = path.name
    if path.name == "__init__.py":
        bare = rel.rsplit("/", 1)[0] if "/" in rel else ""
        rel = f"{bare}/__init__.py" if bare else "__init__.py"
    try:
        source = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return []
    return scan_source(source, module=module_for_rel(rel), is_package=_is_package(rel))


def iter_python_files(roots: list[str]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        base = (REPO_ROOT / root) if not Path(root).is_absolute() else Path(root)
        if base.is_file():
            files.append(base)
            continue
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in path.parts):
                continue
            files.append(path)
    return files


# ── 基线 / 白名单 ───────────────────────────────────────────


def load_baseline(path: Path | None = None) -> set[str]:
    """读基线（``规则|文件|细节``；``#`` 起注释，空行忽略）。"""
    target = path or BASELINE_PATH
    if not target.exists():
        return set()
    keys: set[str] = set()
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        keys.add(line)
    return keys


def load_shims(path: Path | None = None) -> dict[str, str]:
    """读兼容壳白名单（``文件  理由``；``#`` 起注释）。"""
    target = path or SHIMS_PATH
    if not target.exists():
        return {}
    entries: dict[str, str] = {}
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        path_part, _, reason = line.partition("#")
        path_part = path_part.strip()
        if path_part:
            entries[path_part] = reason.strip()
    return entries


def write_baseline(violations: list[Violation], path: Path | None = None) -> Path:
    target = path or BASELINE_PATH
    lines = [
        "# 架构边界基线：**已经存在**的违规（存量不要求一次性清零）。",
        "# 格式：规则|文件|细节。新增违规会被 tools/check_architecture.py 阻塞。",
        "# 重构推进时这份文件只应该**变短**；用 --update-baseline 重写它。",
        "",
    ]
    lines.extend(sorted({v.key for v in violations}))
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


@dataclass(slots=True)
class Report:
    """一次扫描的结果分类。"""

    new: list[Violation]
    known: list[Violation]
    exempt: list[Violation]
    scanned_files: int
    blocking_rules: tuple[str, ...]

    @property
    def failed(self) -> bool:
        return bool(self.new)


def check(
    *,
    roots: list[str] | None = None,
    rules: set[str] | None = None,
    block: set[str] | None = None,
    baseline: set[str] | None = None,
    shims: dict[str, str] | None = None,
) -> Report:
    """扫描并分类（供 CLI 与测试共用）。"""
    active = rules or {rule.id for rule in RULES}
    blocking = block if block is not None else {rule.id for rule in RULES if rule.mode == "block"}
    blocking &= active
    known_keys = baseline if baseline is not None else load_baseline()
    shim_map = shims if shims is not None else load_shims()

    files = iter_python_files(roots or list(DEFAULT_ROOTS))
    new: list[Violation] = []
    was: list[Violation] = []
    exempt: list[Violation] = []
    for path in files:
        for violation in scan_file(path):
            if violation.rule not in active:
                continue
            if violation.rule == "4" and violation.path in shim_map:
                exempt.append(violation)
            elif violation.key in known_keys:
                was.append(violation)
            else:
                new.append(violation)
    return Report(
        new=sorted(new, key=lambda v: (v.rule, v.path, v.detail)),
        known=sorted(was, key=lambda v: (v.rule, v.path, v.detail)),
        exempt=sorted(exempt, key=lambda v: (v.rule, v.path, v.detail)),
        scanned_files=len(files),
        blocking_rules=tuple(sorted(blocking)),
    )


def _print_report(report: Report, *, verbose: bool) -> None:
    by_rule: dict[str, list[Violation]] = {}
    for bucket in (report.new, report.known, report.exempt):
        for violation in bucket:
            by_rule.setdefault(violation.rule, []).append(violation)

    print(f"架构边界门禁：扫描 {report.scanned_files} 个文件。\n")
    print(f"{'规则':<4}{'名称':<24}{'模式':<7}{'新增':>5}{'存量':>6}{'豁免':>6}")
    for rule in RULES:
        items = by_rule.get(rule.id, [])
        new_count = sum(1 for v in items if v in report.new)
        known_count = sum(1 for v in items if v in report.known)
        exempt_count = sum(1 for v in items if v in report.exempt)
        mode = "阻塞" if rule.id in report.blocking_rules else ("报告" if rule.mode == "report" else "关闭")
        print(f"{rule.id:<4}{rule.name:<24}{mode:<7}{new_count:>5}{known_count:>6}{exempt_count:>6}")

    if report.new:
        print("\n新增违规（**必须处理**：要么改依赖，要么写进基线并说明）")
        current = ""
        for violation in report.new:
            if violation.path != current:
                print(f"  {violation.path}")
                current = violation.path
            print(f"    [规则 {violation.rule}] {violation.detail}")

    if verbose and report.known:
        print("\n存量违规（基线内，本次不阻塞）")
        current = ""
        for violation in report.known:
            if violation.path != current:
                print(f"  {violation.path}")
                current = violation.path
            print(f"    [规则 {violation.rule}] {violation.detail}")

    if verbose and report.exempt:
        print("\n白名单豁免（兼容壳，P7 删除）")
        for violation in report.exempt:
            print(f"  {violation.path}  [{violation.detail}]")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="分层与边界静态门禁（AST）")
    parser.add_argument("roots", nargs="*", default=list(DEFAULT_ROOTS), help="扫描根目录")
    parser.add_argument("--rules", default="", help="只跑这些规则，例如 1,4")
    parser.add_argument("--block", default="", help="把这些规则临时当阻塞跑（试运行），例如 2,3")
    parser.add_argument("--update-baseline", action="store_true", help="把当前违规全量写入基线")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    parser.add_argument("--verbose", action="store_true", help="打印存量违规与豁免明细")
    parser.add_argument("--list-rules", action="store_true", help="只打印规则表与判定阈值")
    args = parser.parse_args(argv)

    if args.list_rules:
        for rule in RULES:
            print(f"规则 {rule.id} {rule.name} [{rule.mode}]：{rule.summary}")
        print(f"\n兼容壳阈值：零自有行为（只有 import + __all__）+ 跨包 re-export，"
              f"总代码行 ≤ {SHIM_MAX_LINES}（或 ≤ {SHIM_MAX_VERBOSE_LINES}）")
        print(f"白名单：{SHIMS_PATH.relative_to(REPO_ROOT).as_posix()}")
        print(f"基线：  {BASELINE_PATH.relative_to(REPO_ROOT).as_posix()}")
        return 0

    rules = {part.strip() for part in args.rules.split(",") if part.strip()} or None
    unknown = (rules or set()) - set(RULE_BY_ID)
    if unknown:
        print(f"未知规则：{', '.join(sorted(unknown))}")
        return 2
    block = {part.strip() for part in args.block.split(",") if part.strip()} if args.block else None
    if block and (block - set(RULE_BY_ID)):
        print(f"未知规则：{', '.join(sorted(block - set(RULE_BY_ID)))}")
        return 2

    report = check(roots=list(args.roots), rules=rules, block=block)

    if args.update_baseline:
        path = write_baseline(report.new + report.known)
        print(f"已写入基线：{path.relative_to(REPO_ROOT).as_posix()}（{len(report.new) + len(report.known)} 条）")
        return 0

    if args.json:
        print(json.dumps(
            {
                "scanned_files": report.scanned_files,
                "blocking_rules": list(report.blocking_rules),
                "new": [{"rule": v.rule, "path": v.path, "detail": v.detail} for v in report.new],
                "baseline": [{"rule": v.rule, "path": v.path, "detail": v.detail} for v in report.known],
                "exempt": [{"rule": v.rule, "path": v.path, "detail": v.detail} for v in report.exempt],
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 1 if report.failed else 0

    _print_report(report, verbose=args.verbose or bool(report.new))
    if report.failed:
        print(
            "\n修复方式：改依赖方向（走 Port / 公共 DTO / 只读模型），"
            "或在 tools/architecture_baseline.txt 里登记为存量违规（只减不增），"
            "兼容壳则登记到 tools/compat_shims.txt 并写明删除日期。"
        )
        return 1
    print("\n架构边界门禁通过：无新增违规。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
