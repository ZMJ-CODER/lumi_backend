"""ReAct 的**工具执行与失败恢复**：护栏、去重、结果记账、失败排除。

这里的每一条都是"安全行为"，
本层刻意保持窄职责，便于单独读懂与测试：

* **前置读取护栏**：修改/删除类工具必须先成功读过同一目标（``workspace_edit``
  还需要 revision，只能从读拿到）；
* **重复调用熔断**：同一 (工具, 参数) 连续失败两次后不再重试；
* **失败排除**：确定性失败（不可重试且不是"待确认"）的工具从后续候选池排除；
  网络/超时失败保留——模型可能换个工具后重试成功。

名字一律按**实现名**（``_impl_name``）判断，事件里仍发模型可见名。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import ToolMessage

from app.agents.skills.base import SkillResult


class ToolExecutionMixin:
    """工具执行护栏与结果记账（混入 ``OfficeReactRunner``；不定义 ``__init__``）。"""

    def guard_tool_call(self, impl_name: str, args: dict, *, call_id: str, name: str) -> str:
        """执行前护栏：返回**阻断原因**（空串 = 放行）。

        阻断时调用方据此返回一条 ``status="error"`` 的 ToolMessage，不发真实调用。
        """
        key = self._call_key(impl_name, args)
        attempts = self._call_attempts.get(key, 0)
        if attempts >= 2:
            return "相同工具和参数已连续失败两次，已停止重复调用；请先读取更多信息或更换方法。"
        target_key = self._read_target_key(args)
        if self._requires_prior_read(impl_name) and (
            not self._successful_reads
            or (target_key and target_key not in self._successful_reads)
        ):
            return "安全护栏：修改或删除文件前必须先读取目标内容；请先调用 Read/office_doc_read。"
        self._call_attempts[key] = attempts + 1
        return ""

    def mark_successful_read(self, impl_name: str, args: dict) -> None:
        """记录一次成功读取（前置读取护栏据此放行后续修改）。"""
        if not self._is_read_tool(impl_name):
            return
        target_key = self._read_target_key(args)
        if target_key:
            self._successful_reads.add(target_key)
        else:
            self._successful_reads.add(impl_name.casefold())

    def record_tool_result(self, name: str, result: SkillResult | None) -> dict:
        """``after_tool`` 的记账部分：写 records、排除确定性失败、判断是否需要澄清。

        返回 ``{"force_clarification": bool}``——调用方直接把它并进状态更新。
        """
        record = {
            "skill": name,
            "success": bool(result and result.success),
            "error_code": result.error_code if result else "INVALID_ARGS",
            "error": result.error if result else "工具参数不符合要求",
        }
        if result and isinstance(result.metadata, dict) and result.metadata.get("document_selection"):
            record["document_selection"] = result.metadata["document_selection"]
        self.records.append(record)
        # A deterministic contract failure cannot improve on the next
        # round, so exclude it.  Network/timeout failures remain in
        # the pool: the model may retry once after using another tool
        # or receiving fresh context instead of silently losing the
        # capability for the entire request.
        if not record["success"] and result is not None and not result.retryable and record["error_code"] not in {
            "NEEDS_CONFIRMATION",
        }:
            # 失败方法按**实现名**排除（候选池按实现名过滤）；事件里的名字保持
            # 模型可见名，前端展示不受影响。
            self._failed_tools.add(self._impl_name(name))
        needs_clarification = bool(
            result and result.error_code == "INVALID_PARAMS"
            and isinstance(result.metadata, dict)
            and result.metadata.get("user_action_required")
        )
        return {"force_clarification": needs_clarification}

    async def after_tool_node(self, state: dict) -> dict:
        """``after_tool`` 节点：记账 + 推进轮数 + 发 step 完成/失败事件。"""
        message = state["messages"][-1]
        if not isinstance(message, ToolMessage):
            return {"rounds": int(state.get("rounds") or 0) + 1}
        result = self._results.pop(0) if self._results else None
        name = str(message.name or "执行工具")
        if name in {"search_tools", "discover_domain"} and result is None:
            result = SkillResult(success=True, output="工具发现完成")
        update = self.record_tool_result(name, result)
        call_id = str(message.tool_call_id or f"react-{len(self.records)}")
        success = bool(self.records[-1].get("success"))
        self._emit({
            "type": "step",
            "id": call_id,
            "title": name,
            "status": "completed" if success else "failed",
            "tool": name,
            "output": result.output[:1000] if result and result.success else "",
            "error": None if success else self.records[-1].get("error"),
        })
        return {"rounds": int(state.get("rounds") or 0) + 1, **update}

    #: 由 runner 的 ``__init__`` 赋值（这里只做类型提示）。
    _call_attempts: dict[str, int]
    _successful_reads: set[str]
    _failed_tools: set[str]
    _results: list[Any]
    records: list[dict]
    _surface_alias: dict[str, str]


__all__ = ["ToolExecutionMixin"]
