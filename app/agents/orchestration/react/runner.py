"""受控办公 ReAct 执行器（**门面**）：一轮一个工具，按候选窗严格放行。

本模块保留的是**运行流程**：
加载发现会话 → 准备模型 → 组装节点 → 编译图 → 跑一轮 → 收口结果；
具体职责分别在同级模块里：

| 关注点 | 模块 |
| --- | --- |
| 数据形状（状态/结果） | :mod:`~app.agents.orchestration.react.state` |
| 工具发现、名字判定、领域申请 | :mod:`~app.agents.orchestration.react.tool_selection` |
| 候选窗流水线（领域/原语/缓存/盘点） | :mod:`~app.agents.orchestration.react.tool_window_pipeline` |
| 工作区阶段工具窗口 | :mod:`~app.agents.orchestration.react.workspace_window` |
| 执行护栏、去重、失败排除 | :mod:`~app.agents.orchestration.react.tool_execution` |
| 进度与结果投影 | :mod:`~app.agents.orchestration.react.progress` |
| 系统提示词 | :mod:`~app.agents.orchestration.react.prompt` |
| 状态机装配（节点/边/路由） | :mod:`~app.agents.orchestration.react.graph` |
| 决策轮与收尾轮节点 | :mod:`~app.agents.orchestration.react.agent_node` |

``OfficeReactRunner`` 仍然是**对外门面**（``app/agents/roles/react.py`` 与既有测试
都按这个名字使用），但不再自己承载上述细节；``run()`` 只剩"加载 → 建模型 → 装配 →
跑一轮 → 收口"。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agents.langchain.models import cached_tool_support, get_chat_model
from app.agents.orchestration.react.agent_node import AgentNodeMixin
from app.agents.orchestration.react.graph import build_graph
from app.agents.orchestration.react.progress import ProgressMixin
from app.agents.orchestration.react.prompt import build_system_prompt
from app.agents.orchestration.react.state import ReactRunResult
from app.agents.orchestration.react.tool_execution import ToolExecutionMixin
from app.agents.orchestration.react.tool_selection import ToolSelectionMixin
from app.agents.orchestration.react.workspace_window import WorkspaceWindowMixin
from app.agents.skills.discovery import ToolDiscoverySession
from app.platform.security.agent_security import redact_server_text
from app.services.tool_output_pipeline import clean_assistant_text


class OfficeReactRunner(
    AgentNodeMixin,
    ToolSelectionMixin,
    WorkspaceWindowMixin,
    ToolExecutionMixin,
    ProgressMixin,
):
    """M3 专用的单节点 ReAct 循环，每轮最多放行一个工具调用。"""

    def __init__(self, *, user_id: str, job_id: str, user_role: str = "user",
                 api_key: str | None = None, model: str | None = None,
                 base_url: str | None = None, llm_config: dict[str, Any] | None = None,
                 max_rounds: int = 6,
                 on_progress=None, user_request: str = "",
                 approval_context_sha256: str = "",
                 autonomous_mode: bool = False,
                 max_elapsed_seconds: float | None = None,
                 initial_domain: str = "",
                 initial_mode: str = "read_only",
                 domain_first: bool = False,
                 workspace_id: str = "",
                 workspace_summary: str = "",
                 action_intents: Sequence[str] | None = None) -> None:
        self.user_id = user_id
        self.job_id = job_id
        self.user_role = user_role
        self.api_key = api_key
        self.model_name = model
        self.base_url = base_url
        self.llm_config = llm_config
        # Keep a finite safety ceiling while allowing autonomous rolling
        # tasks to perform explore -> act -> verify -> repair cycles.
        self.max_rounds = min(20, max(1, int(max_rounds)))
        self.on_progress = on_progress
        self.user_request = str(user_request or "")
        self.approval_context_sha256 = str(approval_context_sha256 or "")
        self.autonomous_mode = bool(autonomous_mode)
        self.max_elapsed_seconds = float(max_elapsed_seconds or (900 if self.autonomous_mode else 600))
        self.records: list[dict] = []
        self.citations: list[dict] = []
        self._results: list[Any] = []
        self._failed_tools: set[str] = set()
        #: 模型可见名 → 实现名（Phase 5 收敛；每轮按候选池重建，关闭时为空）
        self._surface_alias: dict[str, str] = {}
        self.toolsets: list[list[str]] = []
        self.selection_traces: list[dict] = []
        self.discovery_session = ToolDiscoverySession()
        self._call_attempts: dict[str, int] = {}
        self._successful_reads: set[str] = set()
        # Domains requested by the model during this rolling run.  A request
        # is only an intent signal; the actual capabilities are still
        # intersected with scene/role/user/Skill policy before injection.
        self._requested_domains: set[str] = set()
        self._active_domain: str | None = None
        self._domain_history: list[str] = []
        self.max_domain_transitions = 4
        self._domain_mode = str(initial_mode or "read_only").casefold()
        self.domain_first = bool(domain_first)
        # Server-authorized local workspace.  When set, the generic read
        # domain (workspace_catalog/list/read/search) may enter the tool
        # window for workspace-related steps; every MCP call is bound to this
        # workspace and routed to its registered desktop.
        self.workspace_id = str(workspace_id or "").strip()
        self.workspace_summary = str(workspace_summary or "")
        # 方案 4 §4.1：画像驱动的工具窗口。给了动作意图就以它为准，关键词只作极端兜底
        # （模型在工具窗口内做出选择，但"窗口里有哪些工具"不再靠猜用户原文）。
        self.action_intents: tuple[str, ...] = tuple(
            str(getattr(item, "value", item)).strip().upper()
            for item in (action_intents or ())
            if str(getattr(item, "value", item)).strip()
        )
        self._workspace_read_caps: list[Any] | None = None
        #: 本次运行的状态（`run()` 开始时写入；节点实现见 react/agent_node.py）。
        self._model: Any = None
        self._cache_model: Any = None
        self._cache_base: Any = None
        self._native_tools_supported = True
        self._instruction = ""
        self._internal_docs: list[dict] = []
        if initial_domain:
            aliases = {"network": "research", "web": "research", "file": "document", "files": "document", "code": "development"}
            normalized = aliases.get(str(initial_domain).strip().casefold(), str(initial_domain).strip().casefold())
            self._requested_domains.add(normalized)
            self._active_domain = normalized
            self._domain_history.append(normalized)

    async def run(self, instruction: str, office_docs: list[dict] | None = None) -> ReactRunResult:
        try:
            await self.discovery_session.load(self.user_id, self.job_id)
            if self.workspace_id and not self.workspace_summary:
                try:
                    from app.workspace.context import (
                        load_workspace_context,
                        workspace_summary_text,
                    )

                    wctx = await load_workspace_context(
                        self.user_id, workspace_id=self.workspace_id
                    )
                    self.workspace_summary = workspace_summary_text(wctx)
                except Exception:  # noqa: BLE001 - 摘要缺失时仍可运行，只读工具调用受授权门约束
                    self.workspace_summary = ""
            model = await get_chat_model(
                scene="office", user_id=self.user_id, api_key=self.api_key,
                model=self.model_name, base_url=self.base_url,
                llm_config=self.llm_config,
            )
            cache_model = self.model_name or getattr(model, "model_name", None) or getattr(model, "model", None)
            cache_base = self.base_url or getattr(model, "openai_api_base", None) or getattr(model, "base_url", None)
            internal_docs = [
                {"doc_id": str(item.get("doc_id")), "filename": str(item.get("filename") or "")}
                for item in (office_docs or [])
                if item.get("doc_id")
            ]
            # 节点实现移到 react/agent_node.py（AgentNodeMixin）：这里只把一次运行需要的
            # 输入写成实例字段，之后不再有闭包。
            self._model = model
            self._cache_model = cache_model
            self._cache_base = cache_base
            self._native_tools_supported = cached_tool_support(cache_model, cache_base) is not False
            self._instruction = instruction
            self._internal_docs = internal_docs

            graph = build_graph(
                agent=self.agent_node,
                before_tool=self.before_tool_node,
                execute_tool=self.execute_tool_node,
                after_tool=self.after_tool_node,
                finish=self.finish_node,
                max_rounds=self.max_rounds,
            )
            system = build_system_prompt(self, internal_docs=internal_docs)
            state = await asyncio.wait_for(
                graph.ainvoke({
                    "messages": [SystemMessage(content=system), HumanMessage(content=instruction)],
                    "rounds": 0,
                    "force_clarification": False,
                }),
                timeout=max(1.0, self.max_elapsed_seconds),
            )
            final = ""
            for message in reversed(state.get("messages") or []):
                if isinstance(message, AIMessage) and not message.tool_calls:
                    final = str(message.content or "")
                    break
            if not final and self.records:
                final = "任务已执行，但模型未生成总结。请查看已完成步骤和产物。"
            for trace, record in zip(self.selection_traces, self.records, strict=False):
                trace["model_called"] = record.get("skill")
                trace["not_called_candidates"] = [
                    item.get("name") for item in trace.get("injected_candidates", [])
                    if item.get("name") and item.get("name") != record.get("skill")
                ]
            return ReactRunResult(
                bool(final or self.records), clean_assistant_text(redact_server_text(final)), records=self.records,
                citations=self.citations, selection_traces=self.selection_traces,
                metrics={
                    "rounds": len(self.records),
                    "tool_failures": sum(1 for item in self.records if not item.get("success")),
                    "candidate_windows": len(self.toolsets),
                    "autonomous_mode": self.autonomous_mode,
                    "active_domain": self._active_domain,
                    "domain_history": list(self._domain_history),
                },
            )
        except asyncio.TimeoutError:
            return ReactRunResult(
                False,
                error="自主执行超过时间预算，已安全停止；可缩小任务范围后继续。",
                error_code="REACT_TIME_BUDGET_EXCEEDED",
                records=self.records,
                citations=self.citations,
                selection_traces=self.selection_traces,
                metrics={"rounds": len(self.records), "autonomous_mode": self.autonomous_mode, "timed_out": True},
            )
        except Exception as exc:  # noqa: BLE001
            try:
                from app.agents.skills.recovery import classify_model_error

                code, message = classify_model_error(exc)
            except Exception:  # noqa: BLE001
                code, message = "REACT_ERROR", str(exc)[:500] or "ReAct 执行失败"
            return ReactRunResult(
                False, error=message, error_code=code, records=self.records,
                citations=self.citations, selection_traces=self.selection_traces,
            )


__all__ = ["OfficeReactRunner"]
