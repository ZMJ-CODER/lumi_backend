"""``code.scan`` 与代码读取增强回归：结构提取、行区间精读、能力/工具登记。

覆盖三件事：

1. **纯函数提取器**：Python 走 ``ast``（类/方法/嵌套/导入/行区间/首行 doc 精确），
   其它语言走保守正则；语法错误**如实报告**而不是伪装成空文件；
2. **``scan`` 动作**：给路径拿骨架、按 ``kind`` 过滤、按 ``find`` 定位行区间、
   按行区间扫局部，且**不返回函数体**；
3. **能力与工具登记**：``code.scan@1`` 在目录里（只读/local_only/免审批）、
   工具映射认它、Provider 归属正确（客户端代码域）。
"""

from __future__ import annotations

import asyncio

import pytest

from lumi_contracts.plugins import DataLocality, Deployment, SideEffectKind

from app.agents.capabilities.registry.builtin import capability_for_tool
from app.agents.capabilities.catalog.legacy import (
    CAPABILITY_CODE_EXECUTE,
    CAPABILITY_CODE_SCAN,
    CAPABILITY_WORKSPACE_READ,
    capability_catalog,
)
from app.knowledge.code.code_structure import (
    find_symbol,
    is_code_language,
    language_for,
    render_skeleton_lines,
    scan_text,
    slice_lines,
    slice_text,
)

PY_SAMPLE = '''"""示例模块：给扫描器吃的。"""
import os
from typing import Any

CONST = 1


class Outer:
    """外层类。"""

    class Inner:
        def deep(self) -> None:
            pass

    def __init__(self, name: str = "x") -> None:
        self.name = name

    async def run(self, items: list[int]) -> dict[str, Any]:
        """跑一遍。"""
        return {"n": len(items)}


def top_level(a: int, b: int = 2) -> int:
    return a + b


def _private() -> None:
    pass
'''

TS_SAMPLE = """import { useState } from 'react';
import fs from 'node:fs';

export class Store {
  save(v: string) { return v; }
}

export function makeStore(name: string) {
  return new Store();
}

const helper = async (x) => x;
"""


def _flat(payload: dict) -> dict[str, dict]:
    """把骨架拍平成 name → 符号（**递归**：嵌套类的方法也在）。"""
    rows: dict[str, dict] = {}

    def _walk(items) -> None:
        for item in items or []:
            rows[item["name"]] = item
            _walk(item.get("children"))

    _walk(payload["symbols"])
    return rows


# ── 1. 纯函数提取器 ──────────────────────────────────────────────


def test_language_detection_covers_code_and_text():
    assert language_for("a.py") == "python"
    assert language_for("a.PY") == "python"
    assert language_for("a.tsx") == "typescript"
    assert language_for("a.go") == "go"
    assert language_for("Makefile") == "text"
    assert is_code_language("python") and not is_code_language("text")


def test_python_scan_extracts_classes_methods_imports_and_line_ranges():
    payload = scan_text(PY_SAMPLE, path="pkg/mod.py")
    assert payload["language"] == "python"
    assert payload["ok"] is True
    rows = _flat(payload)
    # 类 + 嵌套类 + 方法 + 顶层函数都在，且行区间可用
    for name in ("Outer", "Inner", "deep", "__init__", "run", "top_level", "_private", "helper"):
        if name == "helper":
            continue
        assert name in rows, f"缺少符号 {name}"
    outer = rows["Outer"]
    assert outer["kind"] == "class"
    assert outer["line"] < outer["end_line"]
    assert outer["doc"] == "外层类。"          # 只取首行 doc
    assert rows["run"]["kind"] == "method"
    assert rows["run"]["parent"] == "Outer"
    assert rows["run"]["signature"].startswith("async def run(")
    assert rows["run"]["doc"] == "跑一遍。"
    assert rows["top_level"]["kind"] == "function"
    # 签名由 ``ast.unparse`` 规范化给出（保默认值/注解，但空格按 AST 规矩来：
    # 源码里的 ``b: int = 2`` 会归一成 ``b: int=2``）——不搬运源码行，保持确定性。
    assert rows["top_level"]["signature"] == "def top_level(a: int, b: int=2) -> int"
    # 导入：import 与 from ... import 都归一成可读名字
    assert "os" in payload["imports"]
    assert "typing.Any" in payload["imports"]
    # 统计
    assert payload["stats"]["classes"] == 2
    assert payload["stats"]["methods"] == 3
    assert payload["stats"]["imports"] == 2


def test_scan_never_returns_function_bodies():
    """骨架不是正文：函数体内容绝不能出现在结果里。"""
    payload = scan_text(PY_SAMPLE, path="pkg/mod.py")
    blob = str(payload)
    assert 'return {"n": len(items)}' not in blob
    assert "return a + b" not in blob
    assert "self.name = name" not in blob


def test_python_syntax_error_is_reported_not_hidden():
    payload = scan_text("def broken(:\n    pass\n", path="bad.py")
    assert payload["ok"] is False
    assert any("语法错误" in note for note in payload["notes"])
    # 仍然给出文件规模，便于判断"这是不是一个真实文件"
    assert payload["stats"]["lines"] == 2


def test_typescript_scan_uses_conservative_regex():
    payload = scan_text(TS_SAMPLE, path="src/store.ts")
    assert payload["language"] == "typescript"
    rows = _flat(payload)
    assert rows["Store"]["kind"] == "class"
    assert rows["makeStore"]["kind"] == "function"
    assert rows["helper"]["kind"] == "function"      # const helper = async (x) => ...
    assert set(payload["imports"]) == {"react", "node:fs"}
    # 正则无法判定结束行：如实置 0，不瞎猜
    assert all(item["end_line"] == 0 for item in payload["symbols"])


def test_unknown_language_is_text_with_a_note():
    payload = scan_text("hello\nworld\n", path="notes.unknownxyz")
    assert payload["language"] == "text"
    assert payload["symbols"] == []
    assert any("未知语言" in note for note in payload["notes"])


def test_scan_truncates_symbols_and_marks_it():
    source = "\n".join(f"def f{i}():\n    pass\n" for i in range(30))
    payload = scan_text(source, path="many.py", max_symbols=10)
    assert len(payload["symbols"]) == 10
    assert payload["truncated"] is True
    assert any("截断" in note for note in payload["notes"])


def test_slice_lines_and_smart_slice():
    source = "\n".join(f"line{i}" for i in range(1, 21))
    window = slice_lines(source, start_line=3, end_line=5)
    assert window["ok"] is True
    assert window["text"] == "line3\nline4\nline5"
    assert window["total_lines"] == 20 and window["truncated"] is True
    # 超界：明确报错而不是给空串
    out_of_range = slice_lines(source, start_line=99, end_line=120)
    assert out_of_range["ok"] is False and "超出文件总行数" in out_of_range["reason"]
    # 智能切片：短文件给全文，给区间就按区间
    assert slice_text(source)["full"] is True
    assert slice_text(source, start_line=18, end_line=20)["full"] is False


def test_find_symbol_locates_nested_and_top_level():
    payload = scan_text(PY_SAMPLE, path="pkg/mod.py")
    assert find_symbol(payload, "run")["line"] > 0
    assert find_symbol(payload, "Inner")["kind"] == "class"
    assert find_symbol(payload, "nope") is None


def test_skeleton_renders_compact_lines_without_bodies():
    """模型读的是紧凑骨架文本（带行区间与嵌套缩进），不是 JSON、更不是正文。"""
    payload = scan_text(PY_SAMPLE, path="pkg/mod.py")
    lines = render_skeleton_lines(payload["symbols"], imports=payload["imports"],
                                  stats=payload["stats"])
    blob = "\n".join(lines)
    assert "统计：2 个类 / 2 个函数 / 3 个方法 / 2 个导入" in blob
    assert "导入：os, typing.Any" in blob
    assert "- Outer [class] L8-20 — 外层类。" in blob
    assert "    - deep [method]" in blob            # 嵌套类的方法：缩进体现层级
    assert 'async def run(self, items: list[int]) -> dict[str, Any]' in blob  # 签名在，正文不在
    assert "return a + b" not in blob and "self.name = name" not in blob


# ── 2. scan 动作（走 NavigatorScanHandler）─────────────────────


class _FakeReader:
    """替身 WorkspaceReader：直接给正文。"""

    def __init__(self, text: str, *, status: str = "success") -> None:
        self._text = text
        self._status = status

    async def read(self, request, *, path="", cursor="", max_chars=0):
        return {
            "status": self._status,
            "summary": f"已读取 {path}",
            "content": [{"source": path, "location": "", "title": path, "text": self._text}],
            "has_more": False,
            "cursor": None,
            "meta": {"format": "text", "parser": "workspace_read", "workspace_version": 1},
        }


def _navigator(monkeypatch, *, text: str, status: str = "success"):
    from app.workspace.read import navigator as wn

    service = wn.WorkspaceNavigatorService(
        user_id="u1", workspace_id="ws-1", conversation_id="c1"
    )
    monkeypatch.setattr(
        wn.NavigatorScanHandler,
        "_fetch",
        lambda self, path: _FakeReader(text, status=status).read("", path=path),
    )
    return service


def _scan(service, args: dict) -> dict:
    from app.workspace.read.navigator import NavigatorScanHandler

    return asyncio.run(NavigatorScanHandler(service).run(args))


def test_scan_action_returns_skeleton_without_body(monkeypatch):
    service = _navigator(monkeypatch, text=PY_SAMPLE)
    payload = _scan(service, {"path": "pkg/mod.py"})
    assert payload["status"] == "ok"
    assert payload["action"] == "scan"
    data = payload["data"]
    assert data["language"] == "python"
    assert data["stats"]["methods"] == 3
    assert data["imports"]
    assert "def top_level" in str(data["symbols"])
    # 正文不在结果里
    assert "return a + b" not in str(payload)
    assert payload["meta"]["parser"] == "ast"
    assert payload["meta"]["total_lines"] == len(PY_SAMPLE.splitlines())


def test_scan_action_model_text_is_a_readable_skeleton(monkeypatch):
    """回灌模型的是可读骨架（含行区间），不能只剩摘要或"没有结果"。"""
    from app.workspace.read.navigator import model_text

    service = _navigator(monkeypatch, text=PY_SAMPLE)
    payload = _scan(service, {"path": "pkg/mod.py"})
    text = model_text(payload)
    assert "[workspace_navigator/scan]" in text
    assert "- Outer [class] L8-20" in text
    assert "async def run(" in text
    assert "没有结果" not in text
    assert "return a + b" not in text


def test_scan_action_filters_by_kind_and_finds_symbol(monkeypatch):
    service = _navigator(monkeypatch, text=PY_SAMPLE)
    classes = _scan(service, {"path": "pkg/mod.py", "kind": "class"})
    assert [item["name"] for item in classes["data"]["symbols"]] == ["Outer"]
    found = _scan(service, {"path": "pkg/mod.py", "find": "run"})
    assert found["data"]["found"]["name"] == "run"
    assert found["data"]["found"]["line"] > 0
    missing = _scan(service, {"path": "pkg/mod.py", "find": "nope"})
    assert missing["data"]["found"] is None
    assert any("未找到" in note for note in missing["data"]["notes"])


def test_scan_action_marks_partial_window(monkeypatch):
    """只扫一段时必须标明 partial，避免模型把局部骨架当成全文。"""
    service = _navigator(monkeypatch, text=PY_SAMPLE)
    payload = _scan(service, {"path": "pkg/mod.py", "start_line": 10, "end_line": 20})
    assert payload["data"]["partial"] is True
    assert payload["meta"]["sliced"] is True
    assert payload["meta"]["start_line"] == 10
    # 只扫到局部：顶层函数不在这一窗里
    assert all(item["name"] != "top_level" for item in payload["data"]["symbols"])


def test_scan_action_requires_path_and_valid_kind(monkeypatch):
    service = _navigator(monkeypatch, text=PY_SAMPLE)
    assert _scan(service, {})["error"]["code"] == "INVALID_PARAMS"
    assert _scan(service, {"path": "pkg/mod.py", "kind": "nonsense"})["error"]["code"] == "INVALID_PARAMS"


def test_scan_action_surfaces_reader_failure(monkeypatch):
    service = _navigator(monkeypatch, text="", status="failed")
    payload = _scan(service, {"path": "missing.py"})
    assert payload["status"] == "error"
    assert payload["error"]["code"] in {"WORKSPACE_READ_FAILED", "WORKSPACE_PATH_NOT_FOUND"}


class _PagedReader:
    """替身 WorkspaceReader：按 cursor 分页返回，最后一页 has_more=False。"""

    chunks: list[str] = []
    never_ends: bool = False

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    async def read(self, request, *, path="", cursor="", max_chars=0):
        index = int(cursor) if str(cursor).isdigit() else 0
        text = self.chunks[index] if index < len(self.chunks) else ""
        last = index >= len(self.chunks) - 1
        return {
            "status": "success",
            "summary": f"已读取 {path}",
            "content": [{"source": path, "location": "", "title": path, "text": text}],
            "has_more": self.never_ends or not last,
            "cursor": None if (last and not self.never_ends) else str(index + 1),
            "meta": {"format": "text", "parser": "workspace_read", "workspace_version": 1},
        }


def _service():
    from app.workspace.read import navigator as wn

    return wn.WorkspaceNavigatorService(
        user_id="u1", workspace_id="ws-1", conversation_id="c1"
    )


def test_scan_follows_pagination_so_late_symbols_are_not_lost(monkeypatch):
    """骨架必须覆盖整份文件：只读第一页会让后半段符号凭空消失。"""
    from app.workspace.read import reader as wr

    reader = _PagedReader
    reader.chunks = ["class First:\n    pass\n", "def second():\n    pass\n"]
    reader.never_ends = False
    monkeypatch.setattr(wr, "WorkspaceReader", _PagedReader)
    payload = _scan(_service(), {"path": "pkg/mod.py"})
    names = [item["name"] for item in payload["data"]["symbols"]]
    assert names == ["First", "second"]
    assert payload["meta"]["pages_read"] == 2
    assert payload["data"]["partial"] is False
    assert payload["meta"]["budget_exhausted"] is False


def test_scan_marks_partial_when_file_is_not_fully_fetched(monkeypatch):
    """抓不完就必须标 partial + 说明，不能把局部骨架说成全文。"""
    from app.workspace.read import reader as wr

    _PagedReader.chunks = ["def a():\n    pass\n", "def b():\n    pass\n"]
    _PagedReader.never_ends = True
    monkeypatch.setattr(wr, "WorkspaceReader", _PagedReader)
    payload = _scan(_service(), {"path": "pkg/huge.py"})
    assert payload["data"]["partial"] is True
    assert payload["meta"]["budget_exhausted"] is True
    assert any("只扫描了前" in note for note in payload["data"]["notes"])


# ── 3. 能力与工具登记 ───────────────────────────────────────────


def test_code_scan_capability_is_read_only_local_and_approval_free():
    descriptor = capability_catalog.require(CAPABILITY_CODE_SCAN)
    assert descriptor.qualified_name == "code.scan@1"
    assert descriptor.data_locality is DataLocality.LOCAL_ONLY
    assert SideEffectKind.READ in descriptor.side_effects
    # 只读能力免审批：与 workspace.read 同域
    assert descriptor.needs_local_confirmation is False
    assert capability_catalog.require(CAPABILITY_WORKSPACE_READ).needs_local_confirmation is False


def test_code_scan_tool_mapping_and_provider_ownership():
    assert capability_for_tool("workspace_code_scan") == "code.scan"
    assert capability_for_tool("mcp__lumi_pc__workspace_code_scan") == "code.scan"
    from app.agents.capabilities import CapabilityRegistry, register_builtin_providers

    registry = CapabilityRegistry()
    register_builtin_providers(registry=registry)
    rows = registry.registrations(CAPABILITY_CODE_SCAN)
    assert [item.provider_id for item in rows] == ["lumi.local.code"]
    assert rows[0].deployment is Deployment.CLIENT
    # 与 code.execute 同 Provider（都是客户端代码域）
    assert [item.provider_id for item in registry.registrations(CAPABILITY_CODE_EXECUTE)] == [
        "lumi.local.code"
    ]


@pytest.mark.parametrize("kind", ["class", "function", "method", "import"])
def test_scan_input_schema_enumerates_kind_values(kind):
    schema = capability_catalog.require(CAPABILITY_CODE_SCAN).input_schema
    assert kind in schema["properties"]["kind"]["enum"]
    assert schema["required"] == ["path"]
