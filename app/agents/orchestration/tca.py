"""Task Complexity Assessment for office-mode orchestration.

The evaluator deliberately scores task shape instead of business domain.  Its
fast rules cover high-confidence requests without an LLM round trip; an
optional classifier can be injected for uncertain requests as the last stage
of the cascade.
"""

from __future__ import annotations

import inspect
import re
from enum import Enum
from typing import Awaitable, Callable

from pydantic import BaseModel, Field

from app.agents.orchestration.intent import (
    MULTI_STEP_RE,
    classify,
    resolve_direct_text_conversion,
    select_named_office_documents,
)


class ComplexityLevel(str, Enum):
    M0 = "m0"
    M1 = "m1"
    M2 = "m2"
    M3 = "m3"


class ExecutionMode(str, Enum):
    DETERMINISTIC = "deterministic"
    RULE_DAG = "rule_dag"
    PLAN_EXECUTE = "plan_execute"
    REACT = "react"


_MODE_BY_LEVEL = {
    ComplexityLevel.M0: ExecutionMode.DETERMINISTIC,
    ComplexityLevel.M1: ExecutionMode.RULE_DAG,
    ComplexityLevel.M2: ExecutionMode.PLAN_EXECUTE,
    ComplexityLevel.M3: ExecutionMode.REACT,
}


class ComplexityScore(BaseModel):
    entity_count: float = Field(ge=0, le=1)
    parameter_explicitness: float = Field(ge=0, le=1)
    dependency: float = Field(ge=0, le=1)
    ambiguity: float = Field(ge=0, le=1)
    history_dependency: float = Field(ge=0, le=1)
    total: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    level: ComplexityLevel
    mode: ExecutionMode
    stage: str = "rules"
    reasons: list[str] = Field(default_factory=list)

    def audit_dict(self) -> dict:
        return self.model_dump(mode="json")


FallbackClassifier = Callable[[dict], ComplexityLevel | str | Awaitable[ComplexityLevel | str]]


_FILENAME_RE = re.compile(
    r"(?iu)([a-z0-9_\-\u4e00-\u9fff][a-z0-9_.\-\u4e00-\u9fff]*\.[a-z0-9]{1,10})"
)
_SYSTEM_RE = re.compile(
    r"(?iu)(?:excel|word|wps|powerpoint|浏览器|网页|邮件|日历|待办|数据库|知识库|项目|应用|软件)"
)
_EXPLICIT_OUTPUT_RE = re.compile(
    r"(?iu)(?:转(?:换)?(?:成|为)|导出(?:为|成)?|保存为|生成|写入|输出|重命名为|发送到)"
)
_OPEN_ENDED_RE = re.compile(
    r"(?iu)(?:分析原因|找出原因|诊断|给出建议|提出方案|深入研究|全面分析|自行决定|评估风险|为什么)"
)
_HISTORY_RE = re.compile(r"(?iu)(?:上次|刚才|之前|继续|照旧|同样格式|按那个|再来一次)")
_DEPENDENCY_RE = re.compile(
    r"(?iu)(?:然后|之后|再|最后|基于|根据.*结果|先.+再|第一步|第二步|第\s*\d+\s*步)"
)
_MULTI_ACTION_RE = re.compile(r"(?iu)(?:并且|同时|还要|另外|以及|分别|且|然后)")
_VAGUE_RE = re.compile(r"(?iu)(?:处理一下|弄一下|看一下|搞一下|优化一下|那个文件|相关内容|自行处理)")
_DATETIME_RE = re.compile(
    r"(?iu)(?:当前日期|当前时间|现在几点|现在时间|今天几号|今天日期|今天是几月几日)"
)


def _clamp(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 3)


def _entity_count(request: str, office_docs: list[dict] | None) -> int:
    named, _, has_named = select_named_office_documents(request, office_docs)
    referenced_files = len(_FILENAME_RE.findall(request))
    file_count = max(len(named), referenced_files) if has_named else referenced_files
    systems = {m.group(0).casefold() for m in _SYSTEM_RE.finditer(request)}
    return max(file_count, len(named)) + len(systems)


class TaskComplexityAssessor:
    """Low-cost cascade: deterministic rules, then optional learned fallback."""

    def __init__(self, fallback_classifier: FallbackClassifier | None = None) -> None:
        self._fallback_classifier = fallback_classifier

    async def assess(
        self,
        request: str,
        *,
        office_docs: list[dict] | None = None,
        prior_summaries: str = "",
    ) -> ComplexityScore:
        text = (request or "").strip()
        reasons: list[str] = []
        entities = _entity_count(text, office_docs)
        entity_score = _clamp(max(0, entities - 1) / 4)

        has_output = bool(_EXPLICIT_OUTPUT_RE.search(text))
        has_named_file = bool(_FILENAME_RE.search(text))
        explicitness = 0.15
        if has_output:
            explicitness += 0.4
        if has_named_file:
            explicitness += 0.3
        if re.search(r"(?iu)(?:txt|csv|xlsx|docx|pdf|json|md|邮件|报告|清单)", text):
            explicitness += 0.15
        explicitness = _clamp(explicitness)

        dependency = 0.0
        if MULTI_STEP_RE.search(text):
            dependency = 0.9
        elif _DEPENDENCY_RE.search(text):
            dependency = 0.65
        elif _MULTI_ACTION_RE.search(text):
            dependency = 0.4

        ambiguity = 0.1
        if _VAGUE_RE.search(text):
            ambiguity += 0.55
        if _OPEN_ENDED_RE.search(text):
            ambiguity += 0.35
        if len(text) < 8 and not has_named_file:
            ambiguity += 0.2
        ambiguity = _clamp(ambiguity)

        history_dependency = 0.0
        if _HISTORY_RE.search(text):
            history_dependency = 0.85 if prior_summaries else 0.65

        direct_conversion = resolve_direct_text_conversion(text, office_docs)
        intent = classify(text, office_docs)
        if direct_conversion:
            level = ComplexityLevel.M0
            confidence = 0.99
            reasons.append("明确的单文件确定性转换")
        elif _DATETIME_RE.search(text) and dependency == 0:
            level = ComplexityLevel.M0
            confidence = 0.98
            reasons.append("确定性的系统信息查询")
        elif intent.get("task_type") == "template" and dependency < 0.8:
            level = ComplexityLevel.M1
            confidence = 0.9
            reasons.append("命中可复用规则流程")
        else:
            total = _clamp(
                entity_score * 0.18
                + (1 - explicitness) * 0.2
                + dependency * 0.25
                + ambiguity * 0.25
                + history_dependency * 0.12
            )
            # Explicit workflows should stay on the plan/execute path even if
            # they contain conversational fillers such as "看一下" or
            # "处理一下".  Sending these requests to the single-node ReAct
            # shortcut collapses a concrete conversion -> validation -> system
            # action DAG into one 120s-bounded node.  ReAct is reserved for
            # genuinely open-ended decisions or history-dependent requests.
            explicit_workflow = bool(
                dependency >= 0.35
                and (has_output or has_named_file or entities >= 1)
            )
            if _OPEN_ENDED_RE.search(text) or (
                ambiguity >= 0.65 and dependency >= 0.4 and not explicit_workflow
            ):
                level = ComplexityLevel.M3
                confidence = 0.82
                reasons.append("成功标准开放或需根据中间结果动态决策")
            elif history_dependency >= 0.6:
                level = ComplexityLevel.M3
                confidence = 0.78
                reasons.append("请求依赖跨任务历史上下文")
            elif dependency >= 0.35 or entities >= 2 or intent.get("task_type") == "semi_structured":
                level = ComplexityLevel.M2
                confidence = 0.82
                reasons.append("包含多实体或有依赖的可规划步骤")
            elif ambiguity >= 0.55:
                level = ComplexityLevel.M3
                confidence = 0.68
                reasons.append("请求依赖上下文或关键信息不明确")
            else:
                level = ComplexityLevel.M2
                confidence = 0.58
                reasons.append("规则无法确定最小充分路径，采用受控规划")

            if confidence < 0.7 and self._fallback_classifier is not None:
                payload = {
                    "request": text[:1000],
                    "entity_count": entities,
                    "parameter_explicitness": explicitness,
                    "dependency": dependency,
                    "ambiguity": ambiguity,
                    "history_dependency": history_dependency,
                }
                classified = self._fallback_classifier(payload)
                if inspect.isawaitable(classified):
                    classified = await classified
                level = (
                    classified
                    if isinstance(classified, ComplexityLevel)
                    else ComplexityLevel(str(classified).lower())
                )
                confidence = 0.8
                reasons.append("低置信规则结果由分类器复核")
                stage = "classifier"
            else:
                stage = "rules"
            return ComplexityScore(
                entity_count=entity_score,
                parameter_explicitness=explicitness,
                dependency=dependency,
                ambiguity=ambiguity,
                history_dependency=history_dependency,
                total=total,
                confidence=confidence,
                level=level,
                mode=_MODE_BY_LEVEL[level],
                stage=stage,
                reasons=reasons,
            )

        total = _clamp(
            entity_score * 0.18
            + (1 - explicitness) * 0.2
            + dependency * 0.25
            + ambiguity * 0.25
            + history_dependency * 0.12
        )
        return ComplexityScore(
            entity_count=entity_score,
            parameter_explicitness=explicitness,
            dependency=dependency,
            ambiguity=ambiguity,
            history_dependency=history_dependency,
            total=total,
            confidence=confidence,
            level=level,
            mode=_MODE_BY_LEVEL[level],
            stage="rules",
            reasons=reasons,
        )


def next_level(level: ComplexityLevel | str) -> ComplexityLevel | None:
    value = ComplexityLevel(level)
    order = list(ComplexityLevel)
    index = order.index(value)
    return order[index + 1] if index + 1 < len(order) else None
