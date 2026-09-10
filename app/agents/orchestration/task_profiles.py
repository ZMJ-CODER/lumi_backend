"""办公任务的通用画像和抽象节点 → 可执行节点编译。

这一层故意不知道“邮件、报销、早报、合同”等业务名词。Planner 只描述目标、
数据来源、复杂度和安全边界；Skill 注册表决定是否存在适合的操作手册。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Literal

from pydantic import BaseModel, Field

from app.core.config import settings
from app.agents.orchestration.models import TaskNode
from lumi_orch.job_spec import NodeExecutionSpec


Goal = Literal["ANSWER", "GENERATE", "RETRIEVE", "ANALYZE", "EXECUTE", "INTERACT"]
Source = Literal[
    "USER_INPUT", "ATTACHED_FILE", "WORKSPACE_READ", "LOCAL_KNOWLEDGE", "PUBLIC_WEB",
    "EXTERNAL_API", "SYSTEM_STATE",
]
Complexity = Literal["ATOMIC", "SEQUENTIAL", "DYNAMIC"]
Safety = Literal["READ_ONLY", "SAFE_WRITE", "RISKY_WRITE", "CRITICAL"]


class TaskProfile(BaseModel):
    """与业务词解耦的任务能力需求契约。"""

    goal: Goal
    required_sources: list[Source] = Field(default_factory=lambda: ["USER_INPUT"])
    complexity: Complexity = "ATOMIC"
    safety_level: Safety = "READ_ONLY"
    has_side_effect: bool = False
    needs_runtime_decision: bool = False
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    entities: dict = Field(default_factory=dict)


class AbstractTaskNode(BaseModel):
    id: str
    name: str
    profile: TaskProfile
    depends_on: list[str] = Field(default_factory=list)
    is_critical: bool = True
    instruction: str = ""


_SAFETY_ORDER = {"READ_ONLY": 1, "SAFE_WRITE": 2, "RISKY_WRITE": 3, "CRITICAL": 4}


def _is_general_text_profile(profile: TaskProfile) -> bool:
    """Keep ordinary prose in the general model rather than a named template.

    A Skill for a specialised procedure should not steal generic writing merely
    because it happens to accept ``GENERATE + USER_INPUT``.  This is the key
    distinction between a reusable Skill handbook and a business-category
    router.
    """
    return (
        profile.goal in {"ANSWER", "GENERATE", "ANALYZE", "INTERACT"}
        and set(profile.required_sources).issubset({"USER_INPUT"})
        and profile.safety_level == "READ_ONLY"
    )


def _legacy_skill_capability(skill) -> tuple[set[str], set[str], str]:
    """Give pre-profile Skills a conservative derived capability.

    This migration shim means current Prompt-as-Code Skills remain usable, while
    new Skills should declare ``provided_goals/provided_sources/safety_level``.
    """
    goals = {str(item).upper() for item in (getattr(skill, "provided_goals", []) or [])}
    sources = {str(item).upper() for item in (getattr(skill, "provided_sources", []) or [])}
    allowed = {str(item) for item in (getattr(skill, "allowed_tools", []) or [])}
    if not goals:
        if allowed & {"web_search", "web_fetch", "query_knowledge", "Read", "Glob", "Grep"}:
            goals.add("RETRIEVE")
        if allowed & {"Calculator", "office_doc_analyze"}:
            goals.add("ANALYZE")
        if not allowed:
            goals.add("GENERATE")
    if not sources:
        if allowed & {"web_search", "web_fetch"}:
            sources.add("PUBLIC_WEB")
        if "query_knowledge" in allowed:
            sources.add("LOCAL_KNOWLEDGE")
        if allowed & {"office_doc_read", "office_doc_analyze", "read_document"}:
            sources.add("ATTACHED_FILE")
        if allowed & {"Bash", "ProcessList", "SystemInfo"}:
            sources.add("SYSTEM_STATE")
        if not sources:
            sources.add("USER_INPUT")
    safety = str(getattr(skill, "safety_level", "") or "")
    if safety not in _SAFETY_ORDER:
        safety = "RISKY_WRITE" if bool(getattr(skill, "write_op", False)) else "READ_ONLY"
    return goals, sources, safety


async def match_workflow_skill(profile: TaskProfile, *, user_id: str, scene: str = "office"):
    """Return all compatible Skill candidates, ordered by policy-independent facts."""
    from app.services.user_workflow_skills import get_visible_workflow_skills

    if _is_general_text_profile(profile):
        return None
    matches: list[tuple[float, str, object, bool]] = []
    required_sources = set(profile.required_sources)
    for skill in await get_visible_workflow_skills(user_id):
        if skill.status != "stable" or not skill.supports_scene(scene):
            continue
        goals, sources, safety = _legacy_skill_capability(skill)
        if profile.goal not in goals or not required_sources.issubset(sources):
            continue
        if _SAFETY_ORDER.get(safety, 0) < _SAFETY_ORDER[profile.safety_level]:
            continue
        # More specific capability coverage wins; name only stabilizes a tie.
        # Explicit declarations outrank the temporary legacy inference shim.
        declared = bool(getattr(skill, "provided_goals", None) or getattr(skill, "provided_sources", None))
        # This is capability coverage, not a user-text relevance score. The
        # StrategyEngine later decides between the already legal candidates.
        specificity = float(100 - len(goals) - len(sources))
        matches.append((specificity, str(skill.name), skill, declared))
    matches.sort(key=lambda item: (-item[0], item[1]))
    return matches


async def compile_abstract_tasks(
    tasks: list[AbstractTaskNode],
    *,
    user_id: str,
    user_request: str,
    scene: str = "office",
    office_docs: list[dict] | None = None,
    strategy_snapshot=None,
) -> list[TaskNode]:
    """Bind abstract steps to a Skill or an explicit LLM fallback.

    A missing Skill is not hidden. The fallback receives the original goal, its
    predecessor results at runtime and its direct successors so it can still
    answer the relevant part while declaring the missing capability.
    """
    children: dict[str, list[AbstractTaskNode]] = defaultdict(list)
    for task in tasks:
        for dependency in task.depends_on:
            children[dependency].append(task)
    nodes: list[TaskNode] = []
    for task in tasks:
        from app.agents.orchestration.strategy_engine import StrategyCandidate, strategy_engine

        # A job is planned against one immutable strategy snapshot.  Later
        # load/unload operations only affect subsequently planned jobs.
        snapshot = strategy_snapshot
        if isinstance(snapshot, dict):
            snapshot = strategy_engine.snapshot_from_payload(snapshot)
        if snapshot is None:
            snapshot = await strategy_engine.snapshot()
        strategy_snapshot_payload = strategy_engine.snapshot_payload(snapshot)
        matches = await match_workflow_skill(task.profile, user_id=user_id, scene=scene)
        skill = None
        selection_meta: dict = {}
        if matches:
            selection = strategy_engine.select_implementation(
                [
                    StrategyCandidate(
                        value=candidate,
                        name=name,
                        capability_specificity=specificity,
                        declared_capability=declared,
                        reliability=getattr(candidate, "success_rate", None),
                        cost=float(getattr(candidate, "cost_estimate", 1.0) or 1.0),
                    )
                    for specificity, name, candidate, declared in matches
                ],
                profile=task.profile,
                snapshot=snapshot,
            )
            if selection is not None:
                skill = selection.candidate.value
                selection_meta = {
                    "strategy_policy_id": selection.policy_id,
                    "strategy_version": snapshot.version,
                    "strategy_mode": selection.mode,
                    "strategy_score": selection.score,
                    "strategy_snapshot": strategy_snapshot_payload,
                }
        downstream = [child.name or child.id for child in children.get(task.id, [])]
        profile_dump = task.profile.model_dump()
        if skill is not None:
            inputs = _workflow_inputs(
                task.profile,
                task.instruction or user_request,
                office_docs=office_docs,
            )
            # A Skill that requires a particular attachment cannot be invoked
            # until the server has supplied an unambiguous authorized target.
            # Do not turn a filename in an LLM response into access.
            if inputs is None:
                skill = None
        if skill is not None:
            # 审批绑定：只有 CRITICAL 在节点启动前强制确认。READ_ONLY/SAFE_WRITE
            # 自动执行；RISKY_WRITE 由具体工具与 ApprovalPolicyEngine 在真正
            # 提交/副作用发生时按授权策略决定，避免整个 Skill 尚未开始就停在
            # 审批上（尤其是工作区工作流：应先在暂存层完成 读取→修改→测试→
            # diff，只在提交/真实写入时判断是否需要确认）。
            safety = task.profile.safety_level
            node_approval = safety == "CRITICAL"
            node_approval_note = (
                "该步骤为最高风险操作，执行前必须确认。" if node_approval else ""
            )
            nodes.append(TaskNode(
                id=task.id,
                name=task.name or f"执行技能：{skill.name}",
                agent="workflow_skill",
                params={
                    "skill_name": skill.name,
                    "inputs": inputs,
                },
                depends_on=task.depends_on,
                approval=node_approval,
                approval_note=node_approval_note,
                execution=(
                    NodeExecutionSpec(
                        resource_class="external_dependency",
                        timeout_seconds=min(
                            86400,
                            max(120, int(settings.MCP_CLIENT_APPROVAL_WAIT_S) + 30),
                        ),
                    )
                    if str(getattr(skill, "execution_scope", "") or "") in {"client", "backend_orchestrates_client"}
                    else NodeExecutionSpec()
                ),
                metadata={
                    "abstract_profile": profile_dump,
                    "implementation": "skill",
                    "skill_name": skill.name,
                    "safety_level": safety,
                    "approval_boundary": "node_critical_only",
                    "strategy_version": snapshot.version,
                    "strategy_snapshot": strategy_snapshot_payload,
                    **selection_meta,
                },
            ))
            continue
        general_text = _is_general_text_profile(task.profile)
        reason = (
            "该步骤只需要基于用户输入进行通用文本推理，按设计直接由模型完成。"
            if general_text else
            f"当前环境没有匹配此能力画像的 Skill（目标={task.profile.goal}，"
            f"来源={','.join(task.profile.required_sources)}，安全等级={task.profile.safety_level}）。"
        )
        capability_note = (
            "直接完成该可逆文本步骤，不需要外部能力。" if general_text else
            "开头必须简短声明上述能力未安装或未匹配；不得伪称已访问外部来源、文件或系统。"
        )
        instruction = (
            f"用户总目标：{user_request}\n"
            f"当前步骤：{task.name}\n"
            f"当前步骤要求：{task.instruction or user_request}\n"
            f"后续仍需衔接的步骤：{'；'.join(downstream) if downstream else '无，完成后直接交付'}\n"
            f"降级原因：{reason}\n"
            "请仅基于用户输入及前置结果，尽可能完成当前步骤中可以可靠完成的部分。"
            + capability_note
            + "随后直接给出与用户总目标相关的可用结果，并为后续步骤保留清晰结论。"
        )
        nodes.append(TaskNode(
            id=task.id,
            name=task.name or "能力降级回答",
            agent="direct_llm",
            params={"instruction": instruction},
            depends_on=task.depends_on,
            metadata={
                "abstract_profile": profile_dump,
                "implementation": "direct_llm",
                "is_fallback": not general_text,
                # DirectLlmAgent uses this explicit execution contract to
                # return an honest partial answer instead of issuing its
                # generic private-data route-upgrade sentinel.
                "allow_missing_capability_answer": not general_text,
                "fallback_reason": reason,
                "downstream_objectives": downstream,
                "critical_capability_missing": task.is_critical and not general_text,
                "strategy_version": snapshot.version,
                "strategy_snapshot": strategy_snapshot_payload,
            },
        ))
    return nodes


def _workflow_inputs(
    profile: TaskProfile,
    instruction: str,
    *,
    office_docs: list[dict] | None,
) -> dict | None:
    """Provide only server-authorized generic inputs to a matched workflow."""
    inputs = {"instruction": instruction, "question": instruction}
    if "ATTACHED_FILE" not in set(profile.required_sources):
        return inputs
    docs = [item for item in (office_docs or []) if isinstance(item, dict) and item.get("doc_id")]
    if len(docs) != 1:
        return None
    # ``doc_id`` comes only from the request's authorized attachment scope.
    inputs.update({"doc_id": str(docs[0]["doc_id"]), "mode": "qa"})
    return inputs
