"""阶段五回归：契约 → TypeScript 类型导出。

两件事必须成立：

1. 仓库里的 ``packages/contracts/ts/lumi-contracts.d.ts`` 与后端模型**始终同步**
   （漂移即失败，避免前端手抄字段）；
2. 生成的类型里**没有悬空引用**（``$ref`` 指向的定义必须一起导出），且覆盖
   SSE / 运行视图 / 工具结果 / 审批 / 错误信封 / 产物引用。
"""

from __future__ import annotations

import importlib.util
import re

from _paths import REPO_ROOT
REPO_ROOT = REPO_ROOT
SCRIPT_PATH = REPO_ROOT / "packages" / "contracts" / "scripts" / "export_ts.py"
TS_PATH = REPO_ROOT / "packages" / "contracts" / "ts" / "lumi-contracts.d.ts"

_SPEC = importlib.util.spec_from_file_location("lumi_contracts_export_ts", SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
export_ts = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(export_ts)

_BUILTIN_TYPES = {"string", "number", "boolean", "null", "unknown", "Record"}


def _declared(content: str) -> set[str]:
    return set(re.findall(r"^export (?:interface|type) ([A-Za-z0-9_]+)", content, re.MULTILINE))


def test_committed_typescript_is_in_sync_with_backend_contracts():
    generated = export_ts.build()
    current = TS_PATH.read_text(encoding="utf-8")
    assert current == generated, "请运行 python packages/contracts/scripts/export_ts.py 重新生成"


def test_export_covers_cross_boundary_contracts():
    content = TS_PATH.read_text(encoding="utf-8")
    declared = _declared(content)
    for name in (
        "StreamEvent",
        "JobRunView",
        "StepView",
        "ExecutionResult",
        "ToolRequest",
        "ErrorEnvelope",
        "ArtifactRef",
        "ApprovalState",
        "RouteDecision",
        "TaskProfile",
        "ExecutionRequest",
        "ServerContext",
    ):
        assert name in declared, f"缺少跨边界类型：{name}"


def test_exported_event_types_match_backend_enum():
    from lumi_contracts import StreamEventType

    content = TS_PATH.read_text(encoding="utf-8")
    match = re.search(r"^export type StreamEventType = (.+);$", content, re.MULTILINE)
    assert match, "缺少 StreamEventType 联合类型"
    exported = {item.strip().strip('"') for item in match.group(1).split("|")}
    assert exported == {item.value for item in StreamEventType}


def test_no_dangling_type_references():
    content = TS_PATH.read_text(encoding="utf-8")
    declared = _declared(content)
    referenced = set(re.findall(r"\b([A-Z][A-Za-z0-9_]*)\b", content))
    dangling = {
        name
        for name in referenced
        if name not in declared
        and name not in _BUILTIN_TYPES
        # 文档注释里的英文单词（大写开头）不算类型引用
        and f"export interface {name}" not in content
        and f"export type {name}" not in content
    }
    # 允许注释中出现的英文短语（如 "TypeScript"）；真正的悬空 $ref 一定是
    # "某字段?: X" 这种形式，这里只检查出现在字段/联合位置的名字。
    inline = set(re.findall(r"[?:|]\s*([A-Z][A-Za-z0-9_]*)\b", content))
    assert not (inline & dangling), f"存在悬空类型引用：{sorted(inline & dangling)}"


def test_generated_header_names_contract_versions():
    content = TS_PATH.read_text(encoding="utf-8")
    for key in ("lumi.stream_event@1", "lumi.job_run_view@1", "lumi.execution_result@1"):
        assert key in content
