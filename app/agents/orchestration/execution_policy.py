"""v2 统一任务画像 → 执行策略映射层（灰度，EXECUTION_POLICY_V2_ENABLED）。

目标（与业务对象词解耦，不再按“文档/PPT/工作区”单独分支）：
  1. 入口（/chat/stream 或 Job 提交）先给出统一任务画像：
       goal / required_sources / complexity / safety_level /
       has_side_effect / needs_runtime_decision / confidence
  2. 纯函数把画像映射为 execution_policy：
       ATOMIC + READ_ONLY + 仅用户材料            → direct_stream
       ATOMIC + 单次只读外部能力（文档/工作区/KB/联网）→ single_tool_then_stream
       ATOMIC + 单次副作用                         → single_action_skill
       SEQUENTIAL                                  → planner_dag
       DYNAMIC                                     → react
  3. M0-M3 仍保留为兼容路由遥测字段，不再单独决定执行路径；
  4. 结果随 routing/SSE 元数据输出（execution_policy/policy_version v2）；
  5. 配置开关关闭时本层不输出（保留旧 M0-M3 / 旧 DAG 语义）。

本模块是纯逻辑层：不读写 Job/Redis，不调用模型。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.agents.orchestration.task_profiles import TaskProfile

# ── 执行策略命名（与前端契约一致）────────────────────────
POLICY_DIRECT_STREAM = "direct_stream"
POLICY_SINGLE_TOOL_THEN_STREAM = "single_tool_then_stream"
POLICY_SINGLE_ACTION_SKILL = "single_action_skill"
POLICY_PLANNER_DAG = "planner_dag"
POLICY_REACT = "react"
POLICIES = frozenset({
    POLICY_DIRECT_STREAM,
    POLICY_SINGLE_TOOL_THEN_STREAM,
    POLICY_SINGLE_ACTION_SKILL,
    POLICY_PLANNER_DAG,
    POLICY_REACT,
})

POLICY_VERSION = "v2"

# 画像 → 路由记录用的顶层键（与方案第 6 点一致）。
POLICY_ROUTING_KEYS = (
    "task_profile",
    "execution_policy",
    "complexity",
    "policy_version",
    "fallback_action",
)

_READ_ONLY = "READ_ONLY"
_SAFE_WRITE = "SAFE_WRITE"
_RISKY_WRITE = "RISKY_WRITE"
_USER_INPUT = "USER_INPUT"

_HIGH_RISK = re.compile(
    r"(?iu)(?:删除|清空|覆盖|替换|发送|提交|发布|部署|安装|卸载|付款|转账|购买|下单|邮件|短信|消息|"
    r"远程|重启|关机|格式化|drop|delete|rm\s|send|publish|deploy|install|pay|transfer)"
)
_GOAL_WORDS = {
    "RETRIEVE": re.compile(r"(?iu)(?:检索|搜索|查一下|查查|查询|查找|资料|信息|天气|价格|汇率|新闻|政策|竞品|web|search|retrieve|看看|读取|读一下)"),
    "ANALYZE": re.compile(r"(?iu)(?:分析|总结|总结一下|归纳|对比|比较|评价|审查|审阅|检查.*(?:是否|问题)|提取.*(?:问题|关键|要点)|为什么|原因)"),
    "GENERATE": re.compile(r"(?iu)(?:生成|创建|撰写|起草|写一(?:份|个)|整理成|输出(?:一份|一段)|改(?:写|得更)|润色|摘要为|翻译|设计|制定)"),
    "EXECUTE": re.compile(r"(?iu)(?:执行|运行|跑一下|启动|安装|部署|提交|保存|导出)"),
    "INTERACT": re.compile(r"(?iu)(?:打开|操作|点击|启动应用|调用|通知|提醒|设置|修改.*配置)"),
}


def normalize_sources(values: Iterable[str] | None) -> list[str]:
    """去空、去重、保序地归一化 required_sources。"""
    seen: set[str] = set()
    out: list[str] = []
    for value in values or []:
        key = str(value or "").strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out or [_USER_INPUT]


def profile_from_dict(profile: dict | TaskProfile | None) -> TaskProfile:
    """把 dict 或 TaskProfile 规整为 TaskProfile（缺失字段保守默认）。"""
    if isinstance(profile, TaskProfile):
        return profile
    data = dict(profile or {})
    # required_sources 未显式给出时保持 USER_INPUT（避免 normalize 兜底破坏语义）。
    data.setdefault("required_sources", [_USER_INPUT])
    return TaskProfile.model_validate(data)


def execution_policy_for_profile(profile: dict | TaskProfile | None) -> str:
    """纯函数：画像 → execution_policy（映射表见模块 docstring）。"""
    model = profile_from_dict(profile)
    complexity = str(model.complexity or "ATOMIC").upper()
    safety = str(model.safety_level or _READ_ONLY).upper()
    sources = normalize_sources(model.required_sources)
    side_effect = bool(model.has_side_effect) or safety != _READ_ONLY
    if complexity == "DYNAMIC":
        return POLICY_REACT
    if complexity == "SEQUENTIAL":
        return POLICY_PLANNER_DAG
    if side_effect:
        return POLICY_SINGLE_ACTION_SKILL
    if sources and set(sources) <= {_USER_INPUT}:
        return POLICY_DIRECT_STREAM
    # ATOMIC + 只读外部能力（文档 / 工作区读取 / KB / 联网）
    return POLICY_SINGLE_TOOL_THEN_STREAM


def _guess_goal(request: str, sources: list[str]) -> str:
    text = str(request or "")
    hits: list[tuple[str, int]] = []
    for goal, pattern in _GOAL_WORDS.items():
        match = pattern.search(text)
        if match:
            hits.append((goal, match.start()))
    if hits:
        hits.sort(key=lambda item: item[1])
        return hits[0][0]
    return "RETRIEVE" if any(source != _USER_INPUT for source in sources) else "ANSWER"


def _guess_safety(request: str, reasons: Iterable[str]) -> str:
    if not any(reason == "side_effect" for reason in reasons):
        return _READ_ONLY
    return _RISKY_WRITE if _HIGH_RISK.search(str(request or "")) else _SAFE_WRITE


@dataclass(slots=True)
class TaskEntrySignals:
    """入口可得的任务信号（全部来自请求/上下文，不做模型分类）。"""

    request: str = ""
    scene: str = "office"
    reasons: tuple[str, ...] = field(default_factory=tuple)
    has_attachments: bool = False
    has_office_docs: bool = False
    workspace_available: bool = False
    web_search_enabled: bool = False
    conversation_has_workspace: bool = False


def assess_profile_from_signals(signals: TaskEntrySignals) -> dict[str, Any]:
    """由入口信号生成统一任务画像（JSON-safe dict）。

    仅在 EXECUTION_POLICY_V2_ENABLED 时被调用方采用；本函数始终是确定性
    启发式 —— 复杂任务真正的画像仍以 Planner/后续轮次 refine 为准。
    """
    reasons = {str(item).strip() for item in (signals.reasons or ())}
    request = str(signals.request or "").strip()

    sources: list[str] = []
    if signals.has_attachments or signals.has_office_docs:
        sources.append("ATTACHED_FILE")
    if signals.workspace_available or signals.conversation_has_workspace:
        sources.append("WORKSPACE_READ")
    if signals.web_search_enabled or "external_capability" in reasons:
        sources.append("PUBLIC_WEB")
    sources = normalize_sources(sources)

    side_effect = "side_effect" in reasons
    runtime_decision = "runtime_decision" in reasons
    dependency = "dependency" in reasons
    if runtime_decision:
        complexity = "DYNAMIC"
    elif dependency or len(sources) >= 2:
        complexity = "SEQUENTIAL"
    else:
        complexity = "ATOMIC"

    goal = _guess_goal(request, sources)
    safety = _guess_safety(request, reasons)
    confidence = 0.9 if complexity == "ATOMIC" else 0.8
    return {
        "goal": goal,
        "required_sources": sources,
        "complexity": complexity,
        "safety_level": safety,
        "has_side_effect": side_effect,
        "needs_runtime_decision": runtime_decision,
        "confidence": confidence,
    }


def policy_meta_from_signals(
    signals: TaskEntrySignals,
    *,
    enabled: bool,
    fallback_action: str | None = None,
) -> dict[str, Any] | None:
    """入口画像 + 策略的完整路由记录（None 表示策略层未启用）。"""
    if not enabled:
        return None
    profile = assess_profile_from_signals(signals)
    policy = execution_policy_for_profile(profile)
    return {
        "task_profile": profile,
        "execution_policy": policy,
        "complexity": str(profile["complexity"]),
        "policy_version": POLICY_VERSION,
        "fallback_action": fallback_action,
    }


def policy_routing_update(meta: dict[str, Any] | None) -> dict[str, Any]:
    """把 policy meta 转成可直接 merge 进 job.routing 的字段子集。"""
    if not meta:
        return {}
    return {key: meta[key] for key in POLICY_ROUTING_KEYS if key in meta}


def policy_meta_public(meta: dict[str, Any] | None) -> dict[str, Any]:
    """对外（SSE/快照）公开的画像字段子集（不暴露内部实现细节）。"""
    if not meta:
        return {}
    profile = meta.get("task_profile") or {}
    return {
        "task_profile": {
            key: profile.get(key)
            for key in (
                "complexity", "goal", "required_sources", "safety_level",
                "has_side_effect", "needs_runtime_decision", "confidence",
            )
            if key in profile
        },
        "execution_policy": meta.get("execution_policy"),
        "policy_version": meta.get("policy_version"),
    }


__all__ = [
    "POLICY_DIRECT_STREAM",
    "POLICY_SINGLE_TOOL_THEN_STREAM",
    "POLICY_SINGLE_ACTION_SKILL",
    "POLICY_PLANNER_DAG",
    "POLICY_REACT",
    "POLICIES",
    "POLICY_VERSION",
    "POLICY_ROUTING_KEYS",
    "TaskEntrySignals",
    "assess_profile_from_signals",
    "execution_policy_for_profile",
    "policy_meta_from_signals",
    "policy_routing_update",
    "policy_meta_public",
]
