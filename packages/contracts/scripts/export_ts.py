"""把跨前后端契约（``lumi_contracts``）导出为 TypeScript 类型声明。

第五阶段要求"MCP/契约 → 前端"共用同一份定义，避免前端手抄字段。做法：
pydantic 模型 → JSON Schema → TypeScript，**不引入额外依赖**（契约包只依赖
pydantic），生成结果随仓库提交，并有一条回归测试保证它不漂移。

用法::

    python packages/contracts/scripts/export_ts.py            # 写出 ts/lumi-contracts.d.ts
    python packages/contracts/scripts/export_ts.py --check    # 只校验是否已同步（CI 用）

模型清单在这里显式列出：只有"跨边界"的类型才会导出，内部类型不外泄。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

CONTRACTS_SRC = Path(__file__).resolve().parents[1] / "src"
OUTPUT_PATH = Path(__file__).resolve().parents[1] / "ts" / "lumi-contracts.d.ts"
HEADER = """/**
 * 由 packages/contracts/scripts/export_ts.py 生成，请勿手工修改。
 *
 * 契约版本：{versions}
 * 后端模型：lumi_contracts（packages/contracts）
 * 重新生成：python packages/contracts/scripts/export_ts.py
 */

"""

# 需要导出的跨边界模型（顺序即输出顺序，便于 diff 阅读）。
EXPORTED_MODELS: tuple[str, ...] = (
    "ServerContext",
    "ErrorEnvelope",
    "ArtifactRef",
    "ExecutionResult",
    "SkillResult",
    "ToolRequest",
    "TaskProfile",
    "RouteDecision",
    "ExecutionRequest",
    "StreamEvent",
    "ProcessLogEntry",
    "StepView",
    "JobRunView",
    "ApprovalState",
    # ── 插件化/能力化（阶段 0 冻结）──
    "PluginManifest",
    "CapabilityDescriptor",
    "CapabilityInvocation",
    "CapabilityResult",
    "ProviderLease",
    "PluginSnapshot",
    "ViewContribution",
)

# 需要导出的**枚举词表**（前端按值分派，必须与后端逐字一致，不能手抄）。
EXPORTED_ENUMS: tuple[str, ...] = (
    "CapabilityStatus",
    "CapabilityErrorCode",
    "DataLocality",
    "Deployment",
    "IsolationLevel",
    "PluginKind",
    "ProviderHealth",
    "SideEffectKind",
    "TrustLevel",
)


def _ref_name(ref: str) -> str:
    return str(ref).rsplit("/", 1)[-1]


def _literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _object_type(schema: dict[str, Any]) -> str:
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        additional = schema.get("additionalProperties")
        if isinstance(additional, dict):
            return f"Record<string, {ts_type(additional)}>"
        return "Record<string, unknown>"
    required = set(schema.get("required") or [])
    lines: list[str] = []
    for name, sub in properties.items():
        optional = "" if name in required else "?"
        lines.append(f"  {name}{optional}: {ts_type(sub)};")
    return "{\n" + "\n".join(lines) + "\n}"


def ts_type(schema: Any) -> str:
    """JSON Schema 片段 → TypeScript 类型表达式。"""
    if not isinstance(schema, dict):
        return "unknown"
    if "$ref" in schema:
        return _ref_name(schema["$ref"])
    if "const" in schema:
        return _literal(schema["const"])
    if "enum" in schema and isinstance(schema["enum"], list):
        return " | ".join(_literal(item) for item in schema["enum"])
    for key in ("anyOf", "oneOf"):
        options = schema.get(key)
        if isinstance(options, list) and options:
            rendered = [ts_type(item) for item in options]
            # 去重但保持顺序（pydantic 会为 Optional 生成 [T, null]）
            unique: list[str] = []
            for item in rendered:
                if item not in unique:
                    unique.append(item)
            return " | ".join(unique)
    if "allOf" in schema and isinstance(schema["allOf"], list):
        return " & ".join(ts_type(item) for item in schema["allOf"])
    kind = schema.get("type")
    if isinstance(kind, list):
        return " | ".join(ts_type({**schema, "type": item}) for item in kind)
    if kind == "array":
        items = schema.get("items")
        return f"{ts_type(items)}[]"
    if kind == "object":
        return _object_type(schema)
    if kind == "string":
        return "string"
    if kind in {"integer", "number"}:
        return "number"
    if kind == "boolean":
        return "boolean"
    if kind == "null":
        return "null"
    return "unknown"


def render(
    schemas: dict[str, dict[str, Any]],
    versions: dict[str, str],
    event_types: tuple[str, ...] = (),
) -> str:
    """JSON Schema 集合 → 完整的 ``.d.ts`` 文本。"""
    parts = [HEADER.format(versions=", ".join(f"{key}={value}" for key, value in sorted(versions.items())))]
    for name in sorted(schemas):
        schema = schemas[name]
        enum = schema.get("enum")
        if isinstance(enum, list) and schema.get("type") == "string":
            parts.append(f"export type {name} = {' | '.join(_literal(item) for item in enum)};\n")
            continue
        description = str(schema.get("description") or "").strip()
        if description:
            parts.append(f"/** {description.splitlines()[0]} */\n")
        parts.append(f"export interface {name} {_object_type(schema)}\n")
    if event_types:
        union = " | ".join(_literal(item) for item in event_types)
        parts.append(
            "/** 已知流式事件类型；前端按 type 分派，**必须忽略未知类型**而不是白屏。 */\n"
            f"export type StreamEventType = {union};\n"
        )
    return "\n".join(parts)


def _contracts() -> Any:
    """导入契约包（源码目录直连，脚本不依赖已安装的发行版）。"""
    if str(CONTRACTS_SRC) not in sys.path:
        sys.path.insert(0, str(CONTRACTS_SRC))
    import lumi_contracts

    return lumi_contracts


def collect() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """收集跨边界模型与它们引用的 ``$defs``（枚举/子模型必须一起导出）。"""
    module = _contracts()
    schemas: dict[str, dict[str, Any]] = {}
    defs: dict[str, dict[str, Any]] = {}
    for name in EXPORTED_MODELS:
        model = getattr(module, name)
        schema = model.model_json_schema()
        nested = schema.pop("$defs", {}) or {}
        schemas[name] = schema
        for def_name, def_schema in nested.items():
            existing = defs.get(def_name)
            if existing is not None and existing != def_schema:
                raise RuntimeError(f"契约类型名冲突：{def_name} 在两处定义不一致")
            defs[def_name] = def_schema
    for name in EXPORTED_ENUMS:
        enum_cls = getattr(module, name)
        values = [str(item.value) for item in enum_cls]
        if name in schemas or name in defs:
            # 模型里已经带出同名枚举定义时必须一致，否则前端会拿到两套值。
            existing = schemas.get(name) or defs.get(name) or {}
            if list(existing.get("enum") or []) != values:
                raise RuntimeError(f"契约枚举冲突：{name} 与模型内定义不一致")
            continue
        defs[name] = {"type": "string", "enum": values}
    return schemas, defs


def build() -> str:
    """收集模型、生成 TypeScript 文本（供脚本与回归测试共用）。"""
    from lumi_contracts import (
        EXECUTION_RESULT,
        JOB_RUN_VIEW,
        ROUTE_DECISION,
        STREAM_EVENT,
        TASK_PROFILE,
        TOOL_REQUEST,
    )

    schemas, defs = collect()
    versions = {
        "ExecutionResult": str(EXECUTION_RESULT),
        "JobRunView": str(JOB_RUN_VIEW),
        "RouteDecision": str(ROUTE_DECISION),
        "StreamEvent": str(STREAM_EVENT),
        "TaskProfile": str(TASK_PROFILE),
        "ToolRequest": str(TOOL_REQUEST),
    }
    # 引用的公共定义（枚举/子模型）与模型合并输出，保证 $ref 都能解析。
    event_types = tuple(str(item.value) for item in _contracts().StreamEventType)
    return render({**defs, **schemas}, versions, event_types)


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 lumi_contracts 的 TypeScript 类型")
    parser.add_argument("--check", action="store_true", help="只校验是否与仓库中的文件一致")
    parser.add_argument("--out", default=str(OUTPUT_PATH), help="输出路径")
    args = parser.parse_args()

    target = Path(args.out)
    content = build()
    if args.check:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if current != content:
            print(f"契约类型未同步：{target}（请运行 python {Path(__file__).name}）", file=sys.stderr)
            return 1
        print(f"契约类型已同步：{target}")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    print(f"已写出 {target}（{len(content.splitlines())} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
