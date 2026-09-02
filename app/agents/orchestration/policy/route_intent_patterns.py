"""加载部署期四通道意图特征使用的受限标记。"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import yaml
from pydantic import BaseModel, Field, ValidationError

from app.core.config import settings
from app.monitoring.context import MonitorContext
from app.monitoring.logger import monitor_logger


class _PairMarkers(BaseModel):
    actions: tuple[str, ...] = Field(min_length=1, max_length=128)
    targets: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=128)
    sources: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=128)
    max_gap: int = Field(default=64, ge=0, le=256)


class _AgentMarkers(BaseModel):
    phrases: tuple[str, ...] = Field(min_length=1, max_length=128)


class _WindowedMarkers(BaseModel):
    """词汇组合及最大字符间隔；匹配语义由 Python 求值器实现。"""

    actions: tuple[str, ...] = Field(default=(), max_length=128)
    targets: tuple[str, ...] = Field(default=(), max_length=128)
    connectors: tuple[str, ...] = Field(default=(), max_length=64)
    followup_actions: tuple[str, ...] = Field(default=(), max_length=128)
    max_gap: int = Field(default=32, ge=0, le=256)


class RouteIntentPatternDocument(BaseModel):
    version: int = 1
    file_operation: _PairMarkers
    rag_operation: _PairMarkers
    agent_operation: _AgentMarkers
    external_operation: _WindowedMarkers = _WindowedMarkers()
    multi_operation: _WindowedMarkers = _WindowedMarkers()
    stateful_reasoning: _WindowedMarkers = _WindowedMarkers()
    factual_document_question: _WindowedMarkers = _WindowedMarkers()


@dataclass(frozen=True, slots=True)
class RouteIntentPatterns:
    file_actions: tuple[str, ...]
    file_targets: tuple[str, ...]
    file_max_gap: int
    rag_actions: tuple[str, ...]
    rag_sources: tuple[str, ...]
    rag_max_gap: int
    agent_phrases: tuple[str, ...]
    external_actions: tuple[str, ...]
    external_targets: tuple[str, ...]
    external_followup_actions: tuple[str, ...]
    external_max_gap: int
    multi_actions: tuple[str, ...]
    multi_connectors: tuple[str, ...]
    multi_followup_actions: tuple[str, ...]
    multi_max_gap: int
    stateful_actions: tuple[str, ...]
    stateful_targets: tuple[str, ...]
    stateful_max_gap: int
    factual_markers: tuple[str, ...]
    factual_max_gap: int


def _fallback() -> RouteIntentPatterns:
    return RouteIntentPatterns((), (), 0, (), (), 0, (), (), (), 0, (), (), (), 0, (), (), 0, (), 0)


@lru_cache(maxsize=1)
def load_route_intent_patterns() -> RouteIntentPatterns:
    path = Path(settings.AGENT_ROUTING_INTENT_PATTERN_PATH)
    try:
        document = RouteIntentPatternDocument.model_validate(
            yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        )
        if document.version != 1:
            raise ValueError(f"unsupported pattern version: {document.version}")
        return RouteIntentPatterns(
            file_actions=document.file_operation.actions,
            file_targets=document.file_operation.targets or (),
            file_max_gap=document.file_operation.max_gap,
            rag_actions=document.rag_operation.actions,
            rag_sources=document.rag_operation.sources or (),
            rag_max_gap=document.rag_operation.max_gap,
            agent_phrases=document.agent_operation.phrases,
            external_actions=document.external_operation.actions,
            external_targets=document.external_operation.targets,
            external_followup_actions=document.external_operation.followup_actions,
            external_max_gap=document.external_operation.max_gap,
            multi_actions=document.multi_operation.actions,
            multi_connectors=document.multi_operation.connectors,
            multi_followup_actions=document.multi_operation.followup_actions,
            multi_max_gap=document.multi_operation.max_gap,
            stateful_actions=document.stateful_reasoning.actions,
            stateful_targets=document.stateful_reasoning.targets,
            stateful_max_gap=document.stateful_reasoning.max_gap,
            factual_markers=document.factual_document_question.actions,
            factual_max_gap=document.factual_document_question.max_gap,
        )
    except (OSError, ValidationError, yaml.YAMLError, ValueError) as exc:
        monitor_logger.error(
            "四通道路由意图词典加载失败，使用内置模式",
            event_type="policy_load_failure",
            category="configuration",
            code="ROUTE_INTENT_PATTERN_LOAD_FAILED",
            context=MonitorContext(component="route_intent_patterns"),
            metadata={"path": str(path), "error": str(exc)[:300]},
            exc_info=exc,
        )
        return _fallback()


def _contains_pair(text: str, left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    folded = (text or "").casefold()
    for first in left:
        for second in right:
            a, b = first.casefold(), second.casefold()
            if (a in folded and b in folded):
                return True
    return False


def has_configured_file_operation(text: str) -> bool:
    patterns = load_route_intent_patterns()
    return _contains_windowed_pair(text, patterns.file_actions, patterns.file_targets, patterns.file_max_gap) or _contains_windowed_pair(text, patterns.file_targets, patterns.file_actions, patterns.file_max_gap)


def has_configured_rag_operation(text: str) -> bool:
    patterns = load_route_intent_patterns()
    return _contains_windowed_pair(text, patterns.rag_actions, patterns.rag_sources, patterns.rag_max_gap) or _contains_windowed_pair(text, patterns.rag_sources, patterns.rag_actions, patterns.rag_max_gap)


def has_configured_agent_operation(text: str) -> bool:
    folded = (text or "").casefold()
    return any(phrase.casefold() in folded for phrase in load_route_intent_patterns().agent_phrases)


def _contains_windowed_pair(text: str, left: tuple[str, ...], right: tuple[str, ...], max_gap: int) -> bool:
    folded = (text or "").casefold()
    for first in left:
        start = 0
        needle = first.casefold()
        while (position := folded.find(needle, start)) >= 0:
            window = folded[position + len(needle): position + len(needle) + max_gap + 1]
            if any(marker.casefold() in window for marker in right):
                return True
            start = position + 1
    return False


def has_configured_external_operation(text: str) -> bool:
    patterns = load_route_intent_patterns()
    return _contains_windowed_pair(text, patterns.external_actions, patterns.external_targets, patterns.external_max_gap) or _contains_windowed_pair(text, patterns.external_followup_actions, patterns.external_targets, patterns.external_max_gap)


def has_configured_multi_operation(text: str) -> bool:
    patterns = load_route_intent_patterns()
    folded = (text or "").casefold()
    if "第" in folded and "步" in folded:
        return True
    # “先…再/然后…”和“读取…并核对…”均由配置词汇与窗口决定。
    folded = (text or "").casefold()
    for action in patterns.multi_actions:
        for connector in patterns.multi_connectors:
            start = folded.find(action.casefold())
            if start < 0:
                continue
            connector_at = folded.find(connector.casefold(), start + len(action))
            if connector_at < 0 or connector_at - start > patterns.multi_max_gap + len(action):
                continue
            if action.casefold() == "先":
                return True
            if any(folded.find(followup.casefold(), connector_at + len(connector)) >= 0 for followup in patterns.multi_followup_actions):
                return True
    return False


def has_configured_stateful_reasoning(text: str) -> bool:
    patterns = load_route_intent_patterns()
    return _contains_windowed_pair(text, patterns.stateful_actions, patterns.stateful_targets, patterns.stateful_max_gap) or any(marker.casefold() in (text or '').casefold() for marker in patterns.stateful_actions)


def has_configured_factual_document_question(text: str) -> bool:
    patterns = load_route_intent_patterns()
    folded = (text or '').casefold()
    return any(marker.casefold() in folded for marker in patterns.factual_markers)
