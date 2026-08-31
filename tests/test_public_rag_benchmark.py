import json

from scripts.benchmark_public_rag import _load_scifact, _mean_ndcg_at_k, _metrics


def test_ndcg_at_10_uses_graded_qrels_and_ranking_order():
    qrels = {"q1": {"best": 3.0, "other": 1.0}}

    ideal = _mean_ndcg_at_k([["best", "other"]], qrels, ["q1"], k=10)
    reversed_order = _mean_ndcg_at_k([["other", "best"]], qrels, ["q1"], k=10)

    assert ideal == 1.0
    assert 0 < reversed_order < 1.0


def test_metrics_include_ndcg_only_when_qrels_and_rankings_are_available():
    basic = _metrics([1], 1)
    scored = _metrics(
        [1],
        1,
        rankings=[["d1"]],
        qrels={"q1": {"d1": 1.0}},
        query_ids=["q1"],
    )

    assert "nDCG@10" not in basic
    assert scored["nDCG@10"] == 1.0


def test_scifact_loader_reads_standard_beir_files_without_importing_beir(tmp_path):
    (tmp_path / "qrels").mkdir()
    (tmp_path / "corpus.jsonl").write_text(
        json.dumps({"_id": "d1", "title": "Title", "text": "Body"}) + "\n", encoding="utf-8"
    )
    (tmp_path / "queries.jsonl").write_text(
        json.dumps({"_id": "q1", "text": "Question"}) + "\n", encoding="utf-8"
    )
    (tmp_path / "qrels" / "test.tsv").write_text("query-id\tcorpus-id\tscore\nq1\td1\t1\n", encoding="utf-8")

    doc_ids, doc_texts, examples, qrels = _load_scifact(tmp_path)

    assert doc_ids == ["d1"]
    assert doc_texts == ["Title\nBody"]
    assert examples == [("q1", "Question", {"d1"})]
    assert qrels == {"q1": {"d1": 1.0}}
