#!/usr/bin/env python
"""静态扫描：禁止高危调用，允许清单组件例外（AST 实现，不用 grep）。

## 为什么必须是 AST

用正则扫 ``subprocess`` / ``eval(`` / ``compile(`` 会产生海量误报，实测本仓库：

* ``compile(`` 命中 176 行，**没有一行**是内建 ``compile``（158 个 ``re.compile``、
  14 个测试里的 ``def _compile``、4 个 ``graph.compile(`` 方法调用）；
* ``eval(`` 命中 19 行，全是 ``await redis.eval(``（Redis Lua）与测试替身；
* ``app/knowledge/parsing/cleaner.py`` 里**字符串形式**的 ``"os.system("`` 是**检测**恶意
  文档内容的规则表，正则会把安全代码判成违规。

``ast`` 直接看调用节点的形状，天然区分 ``re.compile(...)``（``Attribute``）与
``compile(...)``（``Name``），也不看字符串字面量。因此本脚本能做到"零误报 + 真拦截"。

## 规则

1. 禁止的高危调用（见 :data:`FORBIDDEN`）：子进程、动态执行、反序列化、破坏性删除、
   socket/ctypes 直连等；
2. 只有 :data:`ALLOWED_MODULES` 里的文件可以出现这些调用——它们是**受控执行面**
   （sandbox 运行器、配额进程 worker、受控删除helper），不是普通业务模块；
3. ``import`` 别名会被解析（``import subprocess as sp`` / ``from subprocess import run``），
   因此改个名字绕过门禁是无效的；
4. 也检查**裸引用**：``asyncio.to_thread(shutil.rmtree, target)`` 里 ``shutil.rmtree``
   不在调用位置，只按 ``Call`` 扫会漏（实测 ``app/api/v1/user.py`` 就是这种写法）。

## 用法

    python scripts/check_unsafe_calls.py            # 扫默认根，违规退出码 1
    python scripts/check_unsafe_calls.py app        # 只扫指定目录
    python scripts/check_unsafe_calls.py --list-rules
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 默认扫描根（CI 覆盖范围；比现有 ruff 作业更宽：含 plugins/scripts/celery_app）。
DEFAULT_ROOTS: tuple[str, ...] = (
    "app",
    "packages",
    "plugins",
    "scripts",
    "celery_app",
    "tools",
    "tests",
)

#: 跳过目录（构建产物与虚拟环境）。
SKIP_DIRS: frozenset[str] = frozenset(
    {".venv", "venv", "node_modules", "__pycache__", ".git", ".ptmp", ".pytest_cache", ".ruff_cache", "dist", "build"}
)

#: 允许出现高危调用的文件（**受控执行面**，逐个说明理由）。
#:
#: 这份清单必须尽可能短：每多一个条目，就多一处"CI 不再保护"的代码。
ALLOWED_MODULES: dict[str, str] = {
    "app/services/safe_delete.py": "受控删除的唯一实现（全仓库 rmtree/unlink 收口在此）",
    "app/agents/sandbox/local.py": "本地沙箱运行器（子进程隔离 + 工作目录清理）",
    "app/agents/sandbox/docker.py": "Docker 沙箱运行器（可用性探测 + 容器执行）",
    "app/plugins/quota.py": "插件配额进程 worker（超时 kill / 输出计量 / rlimit）",
    "app/agents/orchestration/temporal/runtime.py": "内置 Temporal 开发服务引导（固定 argv）",
    # 本脚本自身：DOC 字符串里含这些名字，且它需要读文件。
    "scripts/check_unsafe_calls.py": "门禁脚本自身",
    # 门禁自己的测试：必须构造违规样本才能证明门禁有效。
    "tests/structure/test_unsafe_call_gate.py": "门禁的对抗性测试（故意包含违规样本）",
    # 唯一需要 exec 的测试：把**产品代码生成的转换脚本**在测试进程里跑一遍，
    # 是"生成的脚本真的能跑"这条断言的唯一实现方式（脚本本身是受测对象）。
    "tests/office/test_office_docs.py": "内联执行受测的办公转换脚本（脚本生成器是受测对象）",
}


@dataclass(frozen=True, slots=True)
class Forbidden:
    """一条禁止规则。"""

    module: str
    names: frozenset[str]
    reason: str
    attr_only: bool = False


#: 高危调用表：``模块 → 禁止的成员``。
FORBIDDEN: tuple[Forbidden, ...] = (
    Forbidden(
        "subprocess",
        frozenset({"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}),
        "子进程执行必须走沙箱 / 配额 worker",
    ),
    Forbidden(
        "asyncio",
        frozenset({"create_subprocess_exec", "create_subprocess_shell"}),
        "异步子进程同样是执行面（沙箱主路径），必须走沙箱",
    ),
    Forbidden(
        "os",
        frozenset({"system", "popen", "execv", "execve", "execvp", "execvpe", "fork", "kill", "killpg", "spawnv", "spawnl"}),
        "进程执行/信号必须走受控入口",
    ),
    Forbidden(
        "shutil",
        frozenset({"rmtree"}),
        "递归删除必须走 app/services/safe_delete",
    ),
    Forbidden(
        "pickle",
        frozenset({"load", "loads", "Unpickler"}),
        "反序列化不可信数据 = 任意代码执行",
    ),
    Forbidden("marshal", frozenset({"load", "loads"}), "反序列化不可信数据"),
    Forbidden("shelve", frozenset({"open"}), "底层就是 pickle"),
    Forbidden("ctypes", frozenset({"CDLL", "cdll", "WinDLL", "windll", "PyDLL"}), "原生库直连绕过所有边界"),
    Forbidden("socket", frozenset({"socket", "create_connection"}), "网络直连必须走受控 HTTP 客户端"),
)

#: 内建函数（``Name`` 形状，不涉及模块）。
FORBIDDEN_BUILTINS: dict[str, str] = {
    "eval": "动态执行不可信字符串",
    "exec": "动态执行不可信字符串",
    "compile": "动态编译不可信源码（注意：re.compile 是合法的方法调用，不在本规则内）",
    "__import__": "动态导入绕过静态依赖检查",
}

#: ``pathlib`` 破坏性方法（``Path.unlink()`` / ``Path.rmdir()``）。
FORBIDDEN_PATH_METHODS: dict[str, str] = {
    "unlink": "删除文件必须走 safe_delete（含边界校验）",
    "rmdir": "删除目录必须走 safe_delete",
}

#: 测试目录里额外放行的内建（理由见 ``_Visitor._builtin_reason``）。
#: 只放宽"动态导入"这一项：它是既有测试脚手架，不是安全边界；
#: ``eval`` / ``exec`` / ``compile`` 在测试里同样禁止。
_TEST_BUILTIN_EXEMPT: frozenset[str] = frozenset({"__import__"})

#: 测试目录里额外放行的**删除类**规则：测试用 ``tmp_path`` / ``basetemp`` 清理自己刚造的
#: 沙箱目录，边界天然受 pytest 的临时目录约束；把它们逐个搬进 safe_delete 只会让测试更难读，
#: 也不会更安全。**执行类**（子进程/动态执行）在测试里一个都不放行。
_TEST_EXEMPT_RULES: frozenset[str] = frozenset({"shutil.rmtree", "Path.unlink", "Path.rmdir"})

#: 读取源码时的字节序标记：``app/platform/security/security.py`` 带 UTF-8 BOM，
#: 用 ``utf-8`` 读出来会变成 U+FEFF 让 ast 报 "invalid non-printable character"。
_BOM = "\ufeff"


def _module_for(path: Path) -> str:
    """相对仓库根的 POSIX 路径（允许清单按这个形状匹配）。"""
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:  # 仓库外的文件（例如临时目录里的对照样本）
        return path.name


def _aliases(tree: ast.AST) -> tuple[dict[str, str], dict[str, str]]:
    """解析导入别名。

    返回 ``(模块别名, 从模块导入的成员)``：

    * ``import subprocess as sp`` → ``{"sp": "subprocess"}``，因此 ``sp.run(...)`` 会被识别；
    * ``from subprocess import run`` → ``{"run": "subprocess.run"}``，因此裸 ``run(...)``
      也会被识别（否则门禁可以被一行 import 绕过）。
    """
    modules: dict[str, str] = {}
    members: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom):
            if not node.module:
                continue
            for alias in node.names:
                members[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return modules, members


def _dotted(node: ast.AST, modules: dict[str, str]) -> str:
    """把 ``Attribute``/``Name`` 链还原成点号路径（别名归一）。

    **基座解析不出来时必须返回空串**，绝不能退化成"只剩属性名"：
    ``get_redis().eval(...)`` 的基座是 ``Call``，若返回 ``"eval"`` 就会被当成
    内建 ``eval`` 误报——这正是 Redis Lua ``eval`` 被误判的原因（实测 5 处）。
    """
    if isinstance(node, ast.Name):
        return modules.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value, modules)
        return f"{head}.{node.attr}" if head else ""
    return ""


class _Visitor(ast.NodeVisitor):
    """收集违规点（调用 + 裸引用）。"""

    def __init__(self, module: str, source: str, *, parents: dict[int, ast.AST] | None = None) -> None:
        self.module = module
        self.lines = source.splitlines()
        self.violations: list[tuple[int, str]] = []
        self._modules: dict[str, str] = {}
        self._members: dict[str, str] = {}
        # 父节点表（按 id 索引）：用于区分"裸引用危险函数"与"调用它"。
        self._parents: dict[int, ast.AST] = parents or {}
        # 明显是 Path 的变量名（``p: Path`` / ``p = Path(...)``）。
        self._path_names: set[str] = set()
        # 测试目录：删除类规则放行（见 _TEST_EXEMPT_RULES 的理由）。
        self._exempt_rules: frozenset[str] = (
            _TEST_EXEMPT_RULES if module.startswith("tests/") else frozenset()
        )

    # ── 判定 ────────────────────────────────────────────────

    def _check_dotted(self, dotted: str, *, lineno: int, via_call: bool) -> None:
        if not dotted:
            return
        if "." in dotted:
            owner, _, tail = dotted.rpartition(".")
            # 找到拥有该成员的最长模块前缀（``os.path.remove`` 之类不误伤，
            # 因为规则按"模块.成员"精确比对）。
            for rule in FORBIDDEN:
                if owner == rule.module or owner.startswith(rule.module + "."):
                    if tail in rule.names:
                        key = f"{rule.module}.{tail}"
                        if key in self._exempt_rules:
                            return
                        self._add(lineno, f"{dotted}(): {rule.reason}" if via_call else f"{dotted}（引用）: {rule.reason}")
                        return
            # ``Path.unlink()`` / ``Path.rmdir()``：只治 Call，避免把
            # ``on_delete=unlink`` 这类合法回调写成违规。
            if via_call and tail in FORBIDDEN_PATH_METHODS and _looks_like_path(
                owner, path_names=self._path_names, modules=self._modules
            ):
                key = f"Path.{tail}"
                if key in self._exempt_rules:
                    return
                self._add(lineno, f"{dotted}(): {FORBIDDEN_PATH_METHODS[tail]}")
            return
        # 无点号的单名：**先看导入别名**，再看内建。
        # 顺序很关键：``redis.eval(...)`` 里 ``redis.eval`` 是 Attribute（上面已处理），
        # 但 ``r = redis; r.eval(...)`` 与 ``eval = redis.eval; eval(...)`` 必须按别名解析，
        # 否则合法的 Redis Lua ``eval`` 会被当成内建 ``eval`` 误报。
        imported = self._members.get(dotted)
        if imported:
            self._check_dotted(imported, lineno=lineno, via_call=via_call)
            return
        if dotted in self._modules:
            return
        reason = self._builtin_reason(dotted)
        if reason:
            self._add(lineno, f"{dotted}(): {reason}")

    def _builtin_reason(self, name: str) -> str:
        """内建高危函数的判定（测试目录里 ``__import__`` 放行）。

        为什么测试目录单独放宽 ``__import__``：那是既有的动态导入式测试脚手架
        （``__import__("time").time()``），既不影响产品代码，也不是安全边界；
        而 ``eval`` / ``exec`` / ``compile`` 在测试里仍严格禁止——它们能把"测试通过"
        变成"没测到"。
        """
        if name in _TEST_BUILTIN_EXEMPT and self.module.startswith("tests/"):
            return ""
        return FORBIDDEN_BUILTINS.get(name, "")

    def _add(self, lineno: int, message: str) -> None:
        text = (self.lines[lineno - 1].strip() if 0 < lineno <= len(self.lines) else "")
        self.violations.append((lineno, f"{message}  ← {text[:120]}"))

    # ── 遍历 ────────────────────────────────────────────────

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, (ast.Name, ast.Attribute)):
            self._check_dotted(_dotted(node.func, self._modules), lineno=node.lineno, via_call=True)
        elif isinstance(node.func, ast.Call):
            # ``foo()()`` 之类：继续向内看
            self.generic_visit(node.func)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        dotted = _dotted(node, self._modules)
        # ``dotted`` 为空表示基座不是可静态解析的名字（例如 ``get_redis().eval``：
        # 基座是 Call）。这类调用与"内建 ``eval``"无关，必须跳过——实测
        # ``await get_redis().eval(LUA, ...)``（Redis Lua）就是靠这条判定免于误报。
        #
        # 只对"裸引用"报警：调用位置已在 visit_Call 里报过。判据是"本节点是不是某个
        # Call 的 func"，因此 ``await get_redis().eval(...)`` 这种多层结构也不会漏。
        if dotted and not self._is_call_func(node):
            self._check_dotted(dotted, lineno=node.lineno, via_call=False)
        self.generic_visit(node)

    def _is_call_func(self, node: ast.AST) -> bool:
        parent = self._parents.get(id(node))
        return isinstance(parent, ast.Call) and parent.func is node


def _looks_like_path(owner: str, *, path_names: set[str], modules: dict[str, str]) -> bool:
    """``owner`` 看起来是不是 ``Path`` 实例。

    三重判据（宽严相济）：

    * 显式路径类型：``Path`` / ``PosixPath`` / ``WindowsPath``；
    * **名字探测**：``p: Path`` 参数、``p = Path(...)`` 赋值、``*_path`` / ``*_dir``
      这类命名——真实代码里路径变量就是这么写的；
    * 命名空间前缀：``self._path.unlink()`` / ``obj.dir.unlink()``。

    最后一条兜底故意偏严：把 ``.unlink()`` 一律要求走 safe_delete，是这个门禁最想
    守住的边界（删除不可回滚），偶尔要求一处重命名比漏掉一次误删划算。
    """
    tail = owner.rsplit(".", 1)[-1]
    if tail in path_names:
        return True
    if tail in {"Path", "PosixPath", "WindowsPath"}:
        return True
    for alias, target in modules.items():
        if alias == tail and target.endswith("pathlib"):
            return True
    if tail.endswith("_path") or tail.endswith("_dir") or tail.endswith("_file"):
        return True
    return tail in {"path", "target", "base", "root", "user_dir", "container", "session"}


def _path_names(tree: ast.AST) -> set[str]:
    """静态收集"明显是 Path 的变量名"（注解 / 赋值 / 形参注解）。"""
    names: set[str] = set()

    def _is_path_expr(node: ast.AST) -> bool:
        if isinstance(node, ast.Call):
            return _dotted(node.func, {}) .rsplit(".", 1)[-1] in {"Path", "PosixPath", "WindowsPath"}
        return False

    def _is_path_ann(node: ast.AST | None) -> bool:
        if isinstance(node, ast.Name):
            return node.id in {"Path", "PosixPath", "WindowsPath"}
        if isinstance(node, ast.Attribute):
            return node.attr in {"Path", "PosixPath", "WindowsPath"}
        if isinstance(node, ast.Subscript):
            return _is_path_ann(node.value)
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if _is_path_ann(node.annotation):
                names.add(node.target.id)
        elif isinstance(node, ast.Assign):
            if _is_path_expr(node.value):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
        elif isinstance(node, ast.arg) and node.annotation is not None:
            if _is_path_ann(node.annotation):
                names.add(node.arg)
    return names


def scan_source(source: str, *, module: str = "<string>") -> list[tuple[int, str]]:
    """扫一段源码，返回 ``[(行号, 说明)]``（供测试与 CI 共用同一实现）。"""
    if module in ALLOWED_MODULES:
        return []
    text = source.lstrip(_BOM)
    try:
        tree = ast.parse(text, filename=module)
    except SyntaxError as exc:  # 语法错误交给 ruff/pytest 报，门禁不重复报
        return [(int(getattr(exc, "lineno", 1) or 1), f"语法错误，无法扫描：{exc.msg}")]
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
            child._parent = parent  # type: ignore[attr-defined]
    visitor = _Visitor(module, text, parents=parents)
    visitor._modules, visitor._members = _aliases(tree)
    visitor._path_names = _path_names(tree)
    visitor.visit(tree)
    return sorted(set(visitor.violations))


def scan_file(path: Path) -> list[tuple[int, str]]:
    module = _module_for(path)
    try:
        source = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        return [(1, f"无法读取：{exc}")]
    return scan_source(source, module=module)


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
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            files.append(path)
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="高危调用静态门禁（AST）")
    parser.add_argument("roots", nargs="*", default=list(DEFAULT_ROOTS), help="扫描根目录（默认全仓库受管目录）")
    parser.add_argument("--list-rules", action="store_true", help="只打印规则与允许清单")
    args = parser.parse_args(argv)

    if args.list_rules:
        print("禁止的模块调用：")
        for rule in FORBIDDEN:
            print(f"  {rule.module}: {', '.join(sorted(rule.names))}  — {rule.reason}")
        print("禁止的内建函数：")
        for name, reason in FORBIDDEN_BUILTINS.items():
            print(f"  {name}()  — {reason}")
        print("禁止的 pathlib 方法：")
        for name, reason in FORBIDDEN_PATH_METHODS.items():
            print(f"  Path.{name}()  — {reason}")
        print("允许清单（受控执行面）：")
        for module, reason in ALLOWED_MODULES.items():
            print(f"  {module}  — {reason}")
        return 0

    files = iter_python_files(list(args.roots))
    failures: list[tuple[str, int, str]] = []
    for path in files:
        for lineno, message in scan_file(path):
            failures.append((_module_for(path), lineno, message))

    if not failures:
        print(f"高危调用门禁通过：扫描 {len(files)} 个文件，0 处违规。")
        return 0

    print(f"高危调用门禁未通过：扫描 {len(files)} 个文件，{len(failures)} 处违规。\n")
    current = ""
    for module, lineno, message in failures:
        if module != current:
            print(f"{module}")
            current = module
        print(f"  L{lineno}: {message}")
    print(
        "\n修复方式：把调用改到受控入口（app/services/safe_delete.py、沙箱、配额 worker）；"
        "确属新的受控执行面时，在 scripts/check_unsafe_calls.py 的 ALLOWED_MODULES 里加条目"
        "并写明理由。"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
