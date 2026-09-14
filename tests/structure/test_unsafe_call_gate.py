"""高危调用门禁的对抗性测试（证明门禁**真的**拦得住，而不是永远通过）。

本文件自身在门禁的允许清单里（``ALLOWED_MODULES``）：它必须包含违规样本，
否则无法证明门禁有效。除此之外全仓库没有第二个能写这些调用形状的测试文件。

每个用例都同时断言两件事：

1. **真违规判定为违规**（含别名绕过、裸引用绕过、异步子进程等真实绕过手法）；
2. **合法写法不误报**（``re.compile`` / ``redis.eval`` / 字符串里的违规词），
   因为"零误报"是这套门禁能被团队接受的前提。
"""

from __future__ import annotations

import importlib.util
import sys

from _paths import REPO_ROOT
REPO_ROOT = REPO_ROOT


def _gate():
    """导入门禁脚本（``scripts/`` 不是包，按文件加载）。"""
    path = REPO_ROOT / "scripts" / "check_unsafe_calls.py"
    spec = importlib.util.spec_from_file_location("_unsafe_call_gate", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["_unsafe_call_gate"] = module
    spec.loader.exec_module(module)
    return module


gate = _gate()


# ── 1. 真违规必须被拦下 ─────────────────────────────────────


def test_subprocess_run_is_flagged():
    found = gate.scan_source("import subprocess\nsubprocess.run(['ls'])\n", module="app/business.py")
    assert found, "business 模块里的 subprocess.run 必须被拦下"
    assert "subprocess.run()" in found[0][1]


def test_import_alias_cannot_bypass_the_gate():
    """``import subprocess as sp`` 是绕过门禁最省事的办法，必须被识别。"""
    found = gate.scan_source("import subprocess as sp\nsp.Popen(['ls'])\n", module="app/business.py")
    assert found, "改别名不能让门禁失效"


def test_from_import_bare_call_is_flagged():
    """``from subprocess import run`` 之后裸调 ``run(...)`` 同样是违规。"""
    found = gate.scan_source("from subprocess import run\nrun(['ls'])\n", module="app/business.py")
    assert found, "from-import 的裸调用必须被拦下"


def test_asyncio_subprocess_is_flagged():
    """沙箱主路径其实是 ``asyncio.create_subprocess_exec``（只查 subprocess 会漏）。"""
    found = gate.scan_source(
        "import asyncio\nasync def f():\n    await asyncio.create_subprocess_exec('ls')\n",
        module="app/business.py",
    )
    assert found, "异步子进程同样属于执行面"


def test_bare_reference_is_flagged():
    """``asyncio.to_thread(shutil.rmtree, target)`` 只按 Call 扫会漏（真实代码里就有）。"""
    found = gate.scan_source(
        "import asyncio, shutil\nasync def f(t):\n    await asyncio.to_thread(shutil.rmtree, t)\n",
        module="app/business.py",
    )
    assert found, "把危险函数当值传递也必须被拦下"


def test_builtin_eval_and_compile_are_flagged():
    assert gate.scan_source("eval('1+1')\n", module="app/business.py")
    assert gate.scan_source("compile('x=1', '<s>', 'exec')\n", module="app/business.py")


def test_pathlib_unlink_is_flagged():
    found = gate.scan_source(
        "from pathlib import Path\ndef f(p: Path):\n    p.unlink()\n", module="app/business.py"
    )
    assert found, "Path.unlink 等价于删除文件，必须走 safe_delete"


def test_cleanup_helpers_are_flagship_violations():
    for source in (
        "import shutil\nshutil.rmtree('/tmp/x')\n",
        "import pickle\npickle.loads(b'')\n",
        "import socket\nsocket.socket()\n",
    ):
        assert gate.scan_source(source, module="app/business.py"), source


# ── 2. 合法写法绝不能被误报（零误报是这套门禁的立足点）──────


def test_re_compile_is_not_a_builtin_compile():
    """``re.compile`` 有 158 处；正则门禁会全部误报，AST 门禁必须放行。"""
    assert gate.scan_source("import re\nPAT = re.compile('a+')\n", module="app/business.py") == []


def test_redis_eval_is_not_a_builtin_eval():
    """``redis.eval(...)`` / ``await get_redis().eval(...)`` 是 Lua 脚本调用，不是内建 eval。"""
    assert gate.scan_source("r.eval('script', 1, 'k')\n", module="app/business.py") == []
    assert (
        gate.scan_source(
            "async def f():\n    return await get_redis().eval('script', 1, 'k')\n",
            module="app/business.py",
        )
        == []
    )


def test_violation_names_inside_strings_are_ignored():
    """``rag/cleaner.py`` 的安全规则表里就是 ``"os.system("`` 这样的**字符串**。"""
    source = (
        "PATTERNS = [r'\\beval\\s*\\(', r'os\\.system\\s*\\(', r'pickle\\.loads\\s*\\(']\n"
        "NOTE = \"never call subprocess.run here\"\n"
    )
    assert gate.scan_source(source, module="app/knowledge/parsing/cleaner.py") == []


def test_allowlisted_controlled_surface_is_exempt():
    """允许清单里的受控执行面（沙箱/配额 worker/safe_delete）必须放行。"""
    body = "import subprocess, shutil\nsubprocess.run(['ls'])\nshutil.rmtree('/tmp/x')\n"
    for module in (
        "app/agents/sandbox/local.py",
        "app/agents/sandbox/docker.py",
        "app/plugins/quota.py",
        "app/services/safe_delete.py",
    ):
        assert gate.scan_source(body, module=module) == [], module


def test_tests_directory_may_clean_tmp_but_not_exec():
    """测试目录放宽"删除类"（tmp_path 清理），但**不放宽**动态执行。"""
    assert gate.scan_source("import shutil\nshutil.rmtree(tmp)\n", module="tests/test_x.py") == []
    assert gate.scan_source("exec('x=1')\n", module="tests/test_y.py"), "测试里也不许 exec"


def test_bom_prefixed_file_is_scanned():
    """``app/platform/security/security.py`` 带 UTF-8 BOM；读成 utf-8 会让 ast 直接报语法错误。"""
    found = gate.scan_source("\ufeffimport subprocess\nsubprocess.run(['ls'])\n", module="app/business.py")
    assert found, "带 BOM 的文件也必须被扫到（而不是以语法错误混过去）"


# ── 3. 真实仓库必须干净（门禁的自我验收）──────────────────


def test_repository_has_no_violations():
    """全仓库扫描必须 0 违规：这条失败时说明**新代码引入了高危调用**。"""
    failures: list[str] = []
    for path in gate.iter_python_files(list(gate.DEFAULT_ROOTS)):
        for lineno, message in gate.scan_file(path):
            failures.append(f"{gate._module_for(path)}:{lineno}: {message}")
    assert failures == [], "高危调用门禁未通过：\n" + "\n".join(failures[:20])


def test_gate_covers_roots_ruff_does_not():
    """门禁覆盖面必须比现有 ruff 作业更宽（plugins/scripts/celery_app 也在内）。"""
    covered = set(gate.DEFAULT_ROOTS)
    assert {"plugins", "scripts", "celery_app"} <= covered
