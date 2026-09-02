"""规划请求的不可变上下文。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class PlanRequestContext:
    """生成任务树所需的全部用户可控输入。"""

    user_id: str
    request: str
    scene: str = "office"
    project_id: str | None = None
    project_ids: tuple[str, ...] = ()
    llm_api_key: str | None = None
    llm_config: dict[str, Any] | None = None
    clarification_answer: str | None = None
    office_docs: tuple[dict[str, Any], ...] = ()
    prior_summaries: str = ""
    recent_messages: tuple[str, ...] = ()
    recent_artifacts: tuple[dict[str, Any], ...] = ()
    previous_plan: dict[str, Any] | None = None
    permissions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_id", str(self.user_id))
        object.__setattr__(self, "request", str(self.request))
        object.__setattr__(self, "scene", str(self.scene or "office"))
        if self.project_id is not None:
            object.__setattr__(self, "project_id", str(self.project_id))
        object.__setattr__(self, "project_ids", tuple(str(value) for value in (self.project_ids or ()) if str(value).strip()))
        object.__setattr__(self, "office_docs", tuple(dict(item) for item in (self.office_docs or ()) if isinstance(item, Mapping)))
        object.__setattr__(self, "prior_summaries", str(self.prior_summaries or ""))
        object.__setattr__(self, "recent_messages", tuple(str(value) for value in (self.recent_messages or ()) if str(value).strip()))
        object.__setattr__(self, "recent_artifacts", tuple(dict(item) for item in (self.recent_artifacts or ()) if isinstance(item, Mapping)))
        if self.previous_plan is not None and not isinstance(self.previous_plan, Mapping):
            object.__setattr__(self, "previous_plan", None)
        elif self.previous_plan is not None:
            object.__setattr__(self, "previous_plan", dict(self.previous_plan))
        if self.llm_config is not None:
            object.__setattr__(self, "llm_config", dict(self.llm_config))
        object.__setattr__(self, "permissions", tuple(str(value) for value in (self.permissions or ()) if str(value).strip()))

    @classmethod
    def from_legacy_args(
        cls, user_id: str, request: str, scene: str = "office", project_id: str | None = None,
        project_ids: list[str] | tuple[str, ...] | None = None, llm_api_key: str | None = None,
        llm_config: Mapping[str, Any] | None = None, clarification_answer: str | None = None,
        office_docs: list[dict] | tuple[dict, ...] | None = None, prior_summaries: str = "",
        recent_messages: list[str] | tuple[str, ...] | None = None,
        recent_artifacts: list[dict] | tuple[dict, ...] | None = None,
        previous_plan: Mapping[str, Any] | None = None, permissions: list[str] | tuple[str, ...] | None = None,
    ) -> "PlanRequestContext":
        return cls(
            user_id=user_id, request=request, scene=scene, project_id=project_id,
            project_ids=tuple(project_ids or ()), llm_api_key=llm_api_key,
            llm_config=dict(llm_config) if llm_config else None, clarification_answer=clarification_answer,
            office_docs=tuple(office_docs or ()), prior_summaries=prior_summaries,
            recent_messages=tuple(recent_messages or ()), recent_artifacts=tuple(recent_artifacts or ()),
            previous_plan=previous_plan, permissions=tuple(permissions or ()),
        )

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "PlanRequestContext":
        return cls.from_legacy_args(
            user_id=values.get("user_id", ""), request=values.get("request", ""), scene=values.get("scene", "office"),
            project_id=values.get("project_id"), project_ids=values.get("project_ids"), llm_api_key=values.get("llm_api_key"),
            llm_config=values.get("llm_config"), clarification_answer=values.get("clarification_answer"),
            office_docs=values.get("office_docs"), prior_summaries=values.get("prior_summaries", ""),
            recent_messages=values.get("recent_messages"), recent_artifacts=values.get("recent_artifacts"),
            previous_plan=values.get("previous_plan"), permissions=values.get("permissions"),
        )

    def with_prior_summaries(self, prior_summaries: str) -> "PlanRequestContext":
        return replace(self, prior_summaries=prior_summaries)

    def with_llm_api_key(self, llm_api_key: str | None) -> "PlanRequestContext":
        return replace(self, llm_api_key=llm_api_key)

    def with_llm_config(self, llm_config: Mapping[str, Any] | None) -> "PlanRequestContext":
        return replace(self, llm_config=dict(llm_config) if llm_config else None)

    def as_legacy_args(self) -> tuple[Any, ...]:
        return (
            self.user_id, self.request, self.scene, self.project_id, list(self.project_ids), self.llm_api_key,
            self.clarification_answer, [dict(item) for item in self.office_docs], self.prior_summaries,
        )
