"""``app.contracts``：契约包的**迁移桥接层**（业务侧统一入口）。

为什么不直接在业务代码里 import ``lumi_contracts``：第一阶段要求"旧路径继续可用、
内部逐渐改为引用新契约"，所以要有一个稳定入口，把两件事一起提供：

1. **再导出**：``lumi_contracts`` 的公开类型（``ExecutionResult`` / ``ToolSpec`` /
   ``StreamEvent`` / 投影注册表…），业务模块只需要 import 本模块；
2. **迁移桥**：把现有 ``ToolOutput`` / MCP 信封 / 第三方结果统一转成
   ``ExecutionResult``（``to_execution_result``），并给出投影快捷函数
   （``project`` / ``project_all`` / ``model_text``）。

使用约定（避免"改造到一半"）：

* **新增代码**直接用 ``ExecutionResult[T]`` + 投影；不要再手写
  ``render_for_model`` 之类的展示逻辑；
* **旧代码**保持返回 ``ToolOutput``，在**边界处**调用 ``to_execution_result()``
   转换一次，裸字典不向下游扩散；
* 工作区读取类结果建议走 ``model_text()``，它是"模型投影 + 预算"的统一出口。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lumi_contracts import *  # noqa: F403 - 契约包公开 API 全部再导出
from lumi_contracts import __all__ as _CONTRACT_EXPORTS
from lumi_contracts import LegacyEnvelopeAdapter, adapt_tool_result, default_projection_registry
from lumi_contracts.execution.result import ExecutionResult
from lumi_contracts.projections import (
    DEFAULT_MODEL_BUDGET,
    ModelProjection,
    ProjectionKind,
)

# 工作区读取的类型化 payload（投影按类型名识别）。
from app.contracts.workspace_result import (
    WorkspaceEntry,
    WorkspaceMatch,
    WorkspaceNavigatorResult,
    WorkspaceSection,
)

# 迁移期的"旧信封"也一并再导出：业务侧只需 import app.contracts 就能同时拿到
# 新契约（ExecutionResult）与旧对象（ToolOutput），避免两处到处找。
from app.agents.skills.output_contract import (
    ArtifactRef as LegacyArtifactRef,
    Citation as LegacyCitation,
    OutputMeta as LegacyOutputMeta,
    ToolOutput as ToolOutput,
)


def to_execution_result(
    value: Any,
    *,
    tool_name: str = "",
    namespace: str = "lumi",
) -> ExecutionResult[Any]:
    """把任意遗留结果（``ToolOutput`` / 执行信封 dict / 第三方对象）转成新契约。

    这是**唯一**允许接触裸字典的地方：转换后下游只看到 ``ExecutionResult``。
    """
    return adapt_tool_result(value, tool_name=tool_name, namespace=namespace)


def execution_result_from_envelope(
    envelope: Any,
    *,
    tool_name: str = "",
    namespace: str = "lumi",
) -> ExecutionResult[Any]:
    """执行信封（跨进程形态的裸字典）→ ``ExecutionResult``。

    与 :func:`to_execution_result` 的区别：本函数声明**输入就是执行信封**，因此
    裸字典的解析只发生在这里（``LegacyEnvelopeAdapter.adapt_envelope``），
    下游不会再有"这个 dict 里有没有 status"之类的猜测。
    """
    return LegacyEnvelopeAdapter(tool_name=tool_name, namespace=namespace).adapt_envelope(envelope)


def to_tool_output(result: ExecutionResult[Any]) -> ToolOutput:
    """``ExecutionResult`` → 旧 ``ToolOutput``（迁移期的回程适配）。

    ``ExecutionResult`` 是内部唯一表示；需要交回给仍按 ``ToolOutput`` 消费的
    旧接口（如技能执行结果、节点结果）时，在这里一次性还原，语义保持一致：

    * ``payload`` → ``data``；``content_type`` / ``call_id`` / ``retryable`` 同名映射；
    * 信封里的 ``meta`` 原样还原（含 ``transport`` / ``workspace_id`` 等扩展字段），
      不再从散字段重新拼装；
    * ``ErrorEnvelope`` → ``error`` / ``error_code``。
    """
    metadata = dict(result.metadata or {})
    raw_meta = metadata.get("meta")
    try:
        meta = LegacyOutputMeta.model_validate(raw_meta) if isinstance(raw_meta, Mapping) else LegacyOutputMeta()
    except (TypeError, ValueError):
        meta = LegacyOutputMeta()
    if not meta.artifact_refs and result.artifact_refs:
        meta = meta.model_copy(
            update={
                "artifact_refs": [
                    LegacyArtifactRef(
                        ref_id=str(ref.ref_id),
                        name=str(ref.name or ""),
                        media_type=str(ref.media_type or "application/octet-stream"),
                        size=ref.size,
                    )
                    for ref in result.artifact_refs
                ]
            }
        )
    error_code = str(getattr(result.error, "code", "") or "")
    error_message = str(getattr(result.error, "message", "") or "")
    content_type = str(result.content_type or "text")
    if content_type not in {"text", "structured", "artifact", "streaming"}:
        content_type = "text"
    output = str(result.output or "")
    updates: dict[str, Any] = {}
    if not meta.total_size and output:
        updates["total_size"] = len(output)
    if str(result.status) == "pending_approval" and not meta.summary:
        # 与旧 ``normalize_skill_result`` 一致：待审批必须有可展示摘要。
        updates["summary"] = "[待审批] 操作已提交，等待用户确认，尚未执行"
    if updates:
        meta = meta.model_copy(update=updates)
    # 不传 metadata：ToolOutput.model_post_init 会用 metadata 里的散字段重建 meta，
    # 那会丢掉 transport/workspace_id 等扩展字段并覆盖 total_size。
    return ToolOutput(
        status=str(result.status),
        call_id=str(result.call_id or "") or None,
        data=result.payload,
        content_type=content_type,
        meta=meta,
        output=output,
        error=error_message or None,
        error_code=error_code or None,
        retryable=bool(result.retryable),
    )


def project(result: ExecutionResult[Any], kind: str = "model", **kwargs: Any) -> dict[str, Any]:
    """按 kind 投影（``model`` / ``ui`` / ``audit`` / ``storage``）。"""
    registry = default_projection_registry(**kwargs) if kwargs else default_projection_registry()
    return registry.project(kind, result)


def project_all(result: ExecutionResult[Any]) -> dict[str, dict[str, Any]]:
    """一次性产出四类投影。"""
    return default_projection_registry().project_all(result)


def _workspace_envelope_of(value: Any) -> dict:
    """从任意遗留形态里找出 navigator 统一信封本身。

    统一信封可能出现在三个位置：值本身就是信封（裸字典/``ToolOutput.data``），
    或包在 ``data`` 字段里（``ToolOutput`` 的 payload 已经是内层 ``data``）。
    这里集中判断，避免下游各自猜"拿到的是信封还是信封里的数据"。
    """
    if isinstance(value, dict):
        if is_workspace_envelope(value):
            return value
        nested = value.get("data")
        if isinstance(nested, dict) and is_workspace_envelope(nested):
            return nested
        return {}
    data = getattr(value, "data", None)
    if isinstance(data, dict) and is_workspace_envelope(data):
        return data
    return {}


def to_workspace_result(
    value: Any,
    *,
    tool_name: str = "workspace_navigator",
) -> ExecutionResult[WorkspaceNavigatorResult]:
    """工作区读取结果 → ``ExecutionResult[WorkspaceNavigatorResult]``。

    这是"裸字典只留在 Adapter 内部"的落点：信封在这里被解析成**类型化 payload**，
    下游只见 ``ExecutionResult``，投影层才能按类型名挑到专用投影。

    信封位置由 :func:`_workspace_envelope_of` 统一识别：无论传进来的是裸字典
    信封、还是 ``ToolOutput``（payload 已是内层 ``data``），类型化结果都不会为空。
    """
    legacy = to_execution_result(value, tool_name=tool_name)
    typed = WorkspaceNavigatorResult.from_envelope(_workspace_envelope_of(value))
    return ExecutionResult[WorkspaceNavigatorResult](
        status=legacy.status,
        payload=typed,
        tool_name=legacy.tool_name or tool_name,
        namespace=legacy.namespace,
        schema_name="lumi.workspace_navigator.result",
        schema_version=1,
        call_id=legacy.call_id,
        request_id=legacy.request_id,
        trace_id=legacy.trace_id,
        job_id=legacy.job_id,
        node_id=legacy.node_id,
        error=legacy.error,
        retryable=legacy.retryable,
        partial=legacy.partial,
        timing=legacy.timing,
        artifact_refs=legacy.artifact_refs,
        sensitivity=legacy.sensitivity,
        output=legacy.output,
        content_type=legacy.content_type,
        metadata=legacy.metadata,
    )


def is_workspace_envelope(value: Any) -> bool:
    """是否是工作区 navigator 统一信封（用于在边界选择专用适配）。"""
    envelope = value
    if hasattr(value, "data") and not isinstance(value, dict):
        envelope = getattr(value, "data", None)
    if not isinstance(envelope, dict):
        return False
    return (
        {"status", "action"} <= set(envelope)
        and envelope.get("action") in {"list", "search", "read", "scan"}
        and "data" in envelope
    )


def model_text(result: Any, *, model_budget: int | None = None) -> str:
    """模型投影的文本出口（带回退：没有 text 字段时返回空串）。

    ``model_budget`` 只在首次创建注册表时生效；需要不同预算时请显式构造
    ``ModelProjection(budget=...)``。
    """
    registry = projection_registry(model_budget=model_budget)
    view = registry.project("model", result)
    return str(view.get("text") or "")


# ── 业务侧专用投影（方案第 3 条的扩展点：按 payload 类型注册）──────
#
# 工作区读取结果（``WorkspaceNavigatorResult``）的形状是 {status, action, summary,
# data:{path,sections}, has_more, cursor, meta}。默认模型投影已经能识别 ``sections``，
# 但这里显式注册一个投影，把"读取语义"固定下来：
#   * 正文分段带 [文件 · 位置] 头，模型能直接引用；
#   * 未读完时明确给出 cursor 提示（而不是让模型猜）；
#   * 错误态只给错误码 + 建议，不给内部字段。
class WorkspaceNavigatorModelProjection(ModelProjection):
    """工作区读取结果的模型投影（sections 分段 + 分页提示 + 错误建议）。"""

    def __init__(self, *, budget: int = DEFAULT_MODEL_BUDGET) -> None:
        super().__init__(budget=budget)

    def _project(self, result: Any) -> dict[str, Any]:
        view = super()._project(result)
        payload = getattr(result, "payload", None)
        if payload is None and isinstance(result, dict):
            payload = result.get("payload", result.get("data"))
        # 类型化 payload（WorkspaceNavigatorResult）优先；也兼容未类型化的信封 dict。
        action = str(getattr(payload, "action", "") or "")
        has_more = getattr(payload, "has_more", None)
        cursor = getattr(payload, "cursor", None)
        if isinstance(payload, dict):
            action = action or str(payload.get("action") or "")
            if has_more is None:
                has_more = payload.get("has_more")
            if cursor is None:
                cursor = payload.get("cursor")
        # scan 的骨架是"列表型"数据，通用投影会把它序列化成 JSON（既长又难读）；
        # 这里换成紧凑逐行骨架，模型才能一眼看清结构并据此决定精读哪一段。
        if action == "scan":
            symbols = getattr(payload, "symbols", None)
            imports = getattr(payload, "imports", None)
            stats = getattr(payload, "stats", None)
            notes = getattr(payload, "notes", None)
            if isinstance(payload, dict):
                symbols = symbols or payload.get("symbols")
                imports = imports or payload.get("imports")
                stats = stats or payload.get("stats")
                notes = notes or payload.get("notes")
            from app.knowledge.code.code_structure import render_skeleton_lines

            summary = str(getattr(payload, "summary", "") or "")
            if isinstance(payload, dict):
                summary = summary or str(payload.get("summary") or "")
            skeleton = render_skeleton_lines(symbols, imports=imports, stats=stats)
            if summary:
                skeleton.insert(0, summary)
            for note in list(notes or [])[:3]:
                skeleton.append(f"备注：{note}")
            if skeleton:
                view["text"] = "\n".join(skeleton)
        if action:
            view["action"] = action
        if has_more and cursor:
            view["text"] = f"{view.get('text') or ''}\n（还有更多内容，可用 cursor 继续：{cursor}）"
            view["has_more"] = True
            view["cursor"] = str(cursor)
        return view


def projection_registry(*, model_budget: int | None = None):
    """业务侧投影注册表：契约默认四投影 + 业务专用投影（按需注册一次）。"""
    registry = (
        default_projection_registry(model_budget=model_budget)
        if model_budget
        else default_projection_registry()
    )
    if "WorkspaceNavigatorResult" not in _REGISTERED_TYPES:
        budget = int(model_budget or DEFAULT_MODEL_BUDGET)
        registry.register_for(
            ProjectionKind.MODEL,
            "WorkspaceNavigatorResult",
            WorkspaceNavigatorModelProjection(budget=budget),
        )
        _REGISTERED_TYPES.add("WorkspaceNavigatorResult")
    return registry


_REGISTERED_TYPES: set[str] = set()


__all__ = [
    *_CONTRACT_EXPORTS,
    "LegacyArtifactRef",
    "LegacyCitation",
    "LegacyOutputMeta",
    "ToolOutput",
    "WorkspaceEntry",
    "WorkspaceMatch",
    "WorkspaceNavigatorModelProjection",
    "WorkspaceNavigatorResult",
    "WorkspaceSection",
    "is_workspace_envelope",
    "model_text",
    "project",
    "project_all",
    "projection_registry",
    "execution_result_from_envelope",
    "to_execution_result",
    "to_tool_output",
    "to_workspace_result",
]
