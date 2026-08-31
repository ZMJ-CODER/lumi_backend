from pathlib import Path

from loguru import logger

from app.agents.orchestration.task_routing import RouteChannel
from scripts.aggregate_four_channel_routing_logs import aggregate
from scripts.evaluate_four_channel_routing import evaluate_cases, load_cases, render_markdown


def test_labelled_four_channel_fixture_covers_and_matches_all_routes():
    fixture = Path("tests/fixtures/four_channel_routing_eval.jsonl")

    report = evaluate_cases(load_cases(fixture))

    assert report["case_count"] == 80
    assert report["route_accuracy"] == 1.0
    assert report["actual_route_counts"] == {channel.value: 20 for channel in RouteChannel}
    assert "Confusion matrix" in render_markdown(report)


def test_log_aggregation_only_uses_content_free_route_events(tmp_path):
    log = tmp_path / "app.log"
    log.write_text(
        'ignore this\n'
        'INFO FOUR_CHANNEL_ROUTE_DECISION {"route":"direct_llm","reason":"direct","estimated_tokens":800}\n'
        'INFO FOUR_CHANNEL_ROUTE_DECISION {"route":"rag","reason":"lookup","estimated_tokens":1200}\n'
        'INFO FOUR_CHANNEL_ROUTE_DECISION {bad json}\n',
        encoding="utf-8",
    )

    report = aggregate([log], minimum_events=2)

    assert report["event_count"] == 2
    assert report["sample_sufficient"] is True
    assert report["malformed_event_count"] == 1
    assert report["route_counts"]["direct_llm"] == 1
    assert report["route_shares"]["rag"] == 0.5


def test_routing_handles_read_only_document_wording_and_filename_before_conversion():
    from app.agents.orchestration.task_routing import route_atomic_instruction

    assert route_atomic_instruction("把供应商信息.xlsx 转为 csv。").channel == RouteChannel.DETERMINISTIC_SCRIPT
    assert route_atomic_instruction("根据知识库说明采购流程的审批节点。").channel == RouteChannel.RAG
    assert route_atomic_instruction(
        "根据上传的合同说明违约条款。", has_authorized_documents=True
    ).channel == RouteChannel.RAG
    assert route_atomic_instruction("把下周完成验收改成正式通知语气。").channel == RouteChannel.DIRECT_LLM


def test_manifest_route_telemetry_is_deferred_until_admission():
    """A capacity-rejected manifest must not affect production route shares."""
    from app.agents.orchestration.task_manifest import (
        new_manifest,
        record_manifest_route_decisions,
    )

    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), format="{message}")
    try:
        manifest = new_manifest(["写一段项目欢迎词", "从知识库查询上线日期"])
        assert not any(message.startswith("FOUR_CHANNEL_ROUTE_DECISION {") for message in messages)

        record_manifest_route_decisions(manifest)
        events = [message for message in messages if message.startswith("FOUR_CHANNEL_ROUTE_DECISION {")]
        assert len(events) == 2
    finally:
        logger.remove(sink_id)
