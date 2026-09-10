"""准备规划与执行共用的不可变上下文。

提交代码需要唯一可信的附件视图、有界办公记忆视图以及冻结后的有效模型配置。
集中在这里处理，可避免不同提交分支解析出不同模型或信任不同的客户端附件元
数据。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.orchestration.memory_service import OfficeMemoryService
from app.agents.orchestration.planning.context import PlanRequestContext
from app.core.llm_config import EffectiveLLMConfig, resolve_effective_llm_config


@dataclass(slots=True)
class SubmissionContext:
    office_docs: list[dict]
    pending_office_docs: list[dict]
    prior_summaries: str
    presentation_preferences: str
    effective_llm: EffectiveLLMConfig
    planning_context: PlanRequestContext


class SubmissionContextService:
    """Resolve trusted office inputs and a job-scoped LLM snapshot."""

    def __init__(self, *, memory: OfficeMemoryService) -> None:
        self._memory = memory

    async def prepare(
        self,
        *,
        user_id: str,
        request: str,
        scene: str,
        conversation_id: str | None,
        project_id: str | None,
        project_ids: list[str] | None,
        request_api_key: str | None,
        clarification_answer: str | None,
        office_docs: list[dict] | None,
        workspace_id: str | None = None,
    ) -> SubmissionContext:
        verified_docs: list[dict] = []
        pending_office_docs: list[dict] = []
        prior_summaries = ""
        presentation_preferences = ""
        if scene == "office":
            # Document references are supplied by the client and re-verified
            # against the user's own office sessions below.  Workspace file
            # content is owned by Electron and read through desktop MCP tools,
            # so it is deliberately NOT pre-merged from a server-side mirror.
            merged_docs: list[dict] = []
            seen_doc_ids: set[str] = set()
            for item in office_docs or []:
                if not isinstance(item, dict):
                    continue
                doc_id = str(item.get("doc_id") or "").strip()
                if doc_id and doc_id not in seen_doc_ids:
                    seen_doc_ids.add(doc_id)
                    merged_docs.append(item)
            pending_office_docs = [
                item for item in merged_docs
                if str(item.get("status") or "ready").strip().lower() not in {"", "ready"}
            ]
            verified_docs = await self._memory.verify_documents(
                user_id, request, merged_docs
            )
            # Every office turn gets a small recent-task summary.  Explicit
            # historical references may add a more targeted indexed recall,
            # but they are no longer the only way to recover office context.
            load_summaries = getattr(self._memory, "load_summaries", None)
            recent_summary = (
                await load_summaries(conversation_id or "")
                if callable(load_summaries) else ""
            )
            recalled = await self._memory.load_recall_context(
                user_id, request, conversation_id
            )
            if recent_summary and recalled:
                prior_summaries = recent_summary + "\n" + recalled
            else:
                prior_summaries = recent_summary or recalled
            presentation_preferences = await self._memory.load_presentation_preferences(
                user_id
            )
        # 工作区目录/状态摘要（只读、不含文件正文）：conversation/workspace 都
        # 存在时从 WorkspaceContext 解析并渲染；不可用/降级时文本自带边界说明。
        workspace_summary = ""
        workspace_grant: dict = {}
        if scene == "office" and (workspace_id or conversation_id):
            try:
                from app.services.workspace_context import (
                    load_workspace_context,
                    workspace_permission_profile,
                    workspace_summary_text,
                )

                wctx = await load_workspace_context(
                    user_id,
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                )
                workspace_summary = workspace_summary_text(wctx)
                # 执行授权快照：approval_mode/policy_version/issued_at/expires_at/
                # access_level/available_domains + workspace_id；随 Job 元数据落库。
                workspace_grant = workspace_permission_profile(wctx)
                workspace_grant.update({
                    "user_id": str(user_id),
                    "conversation_id": str(conversation_id or "")[:128],
                    "device_id": str(getattr(wctx, "device_id", "") or ""),
                })
            except Exception:  # noqa: BLE001 - 摘要/快照缺失不阻断规划，保留保守默认
                workspace_summary = ""
                workspace_grant = {}
        effective_llm = await resolve_effective_llm_config(
            scene=scene,
            user_id=user_id,
            request_api_key=request_api_key,
        )
        llm_config = effective_llm.as_dict()
        planning_context = PlanRequestContext.from_legacy_args(
            user_id=user_id,
            request=request,
            scene=scene,
            project_id=project_id,
            project_ids=project_ids,
            workspace_id=workspace_id,
            workspace_summary=workspace_summary,
            workspace_grant=workspace_grant,
            llm_api_key=effective_llm.api_key,
            llm_config=llm_config,
            clarification_answer=clarification_answer,
            office_docs=verified_docs,
            prior_summaries=prior_summaries,
        )
        return SubmissionContext(
            office_docs=verified_docs,
            pending_office_docs=pending_office_docs,
            prior_summaries=prior_summaries,
            presentation_preferences=presentation_preferences,
            effective_llm=effective_llm,
            planning_context=planning_context,
        )
