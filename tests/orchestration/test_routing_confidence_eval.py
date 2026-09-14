import json

from scripts.evaluate_routing_confidence import evaluate


def test_evaluate_routing_confidence_reads_nested_trace(tmp_path):
    source = tmp_path / "routing.jsonl"
    source.write_text(
        "\n".join([
            json.dumps({"correct": True, "selection": {"top_score": 10, "second_score": 2}, "confidence_hint": 0.9}),
            json.dumps({"correct": False, "metadata": {"routing": {"top_score": 3, "second_score": 2.5, "confidence_hint": 0.8}}}),
        ]) + "\n",
        encoding="utf-8",
    )
    report = evaluate(source)
    assert report["sample_count"] == 2
    assert report["margin_bins"]["[5,10)"]["accuracy"] == 1.0
    assert report["confidence_hint"]["sample_count"] == 2
    assert "temperature" in report["confidence_hint"]


def test_evaluate_routing_confidence_counts_malformed_lines(tmp_path):
    source = tmp_path / "routing.jsonl"
    source.write_text('{"correct": "yes"}\nnot-json\n', encoding="utf-8")
    report = evaluate(source)
    assert report["sample_count"] == 0
    assert report["malformed_count"] == 2
