import asyncio

from app.agents.orchestration.tca import ComplexityLevel, TaskComplexityAssessor


def assess(request, *, docs=None, history="", fallback=None):
    return asyncio.run(
        TaskComplexityAssessor(fallback_classifier=fallback).assess(
            request, office_docs=docs, prior_summaries=history
        )
    )


def test_explicit_single_file_conversion_is_m0():
    score = assess(
        "把 scores.csv 转为 txt",
        docs=[{"doc_id": "private-doc-id", "filename": "scores.csv", "type": "text"}],
    )
    assert score.level == ComplexityLevel.M0
    assert score.mode.value == "deterministic"
    assert score.confidence >= 0.95


def test_known_document_workflow_is_m1():
    score = assess(
        "筛选这些发票并生成报销单",
        docs=[{"doc_id": "d1", "filename": "invoice.pdf"}],
    )
    assert score.level == ComplexityLevel.M1
    assert score.mode.value == "rule_dag"


def test_predictable_multistep_task_is_m2():
    score = assess("合并三份周报，然后翻译成英文")
    assert score.level == ComplexityLevel.M2
    assert score.dependency > 0


def test_explicit_workflow_with_conversational_filler_stays_m2():
    score = assess(
        "把 score.csv 转为 excel，然后判定数据是否合规，并且看一下是否需要打开系统工具",
        docs=[{"doc_id": "d1", "filename": "score.csv"}],
    )
    assert score.level == ComplexityLevel.M2
    assert score.mode.value == "plan_execute"


def test_open_ended_analysis_is_m3():
    score = assess("分析销售下滑原因并给出建议")
    assert score.level == ComplexityLevel.M3
    assert score.mode.value == "react"


def test_history_reference_raises_complexity():
    score = assess("按上次那个格式再来", history="上次生成了季度报告")
    assert score.history_dependency >= 0.8
    assert score.level == ComplexityLevel.M3


def test_low_confidence_route_uses_optional_classifier():
    calls = []

    async def fallback(payload):
        calls.append(payload)
        return ComplexityLevel.M1

    score = assess("生成一份报告", fallback=fallback)
    assert calls
    assert score.level == ComplexityLevel.M1
    assert score.stage == "classifier"
