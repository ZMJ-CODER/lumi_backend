import json

from app.agents.orchestration.models import TaskNode
from app.agents.orchestration.plan_cache import (
    build_plan_cache_key,
    decode_plan,
    encode_plan,
)


def _docs(doc_id="doc-secret"):
    return [{"doc_id": doc_id, "filename": "scores.csv", "type": "text"}]


def test_plan_cache_key_is_stable_and_contains_no_raw_identity():
    kwargs = {
        "user_id": "user@example.com",
        "request": "把 scores.csv 转为 txt",
        "scene": "office",
        "user_role": "user",
        "office_docs": _docs(),
        "capability_signature": "abc",
    }
    first = build_plan_cache_key(**kwargs)
    second = build_plan_cache_key(**kwargs)
    assert first == second
    assert "user@example.com" not in first
    assert "scores.csv" not in first
    assert "doc-secret" not in first


def test_old_cache_payload_is_rejected_after_planning_policy_version_change():
    old_payload = {
        "version": 1,
        "nodes": [{"id": "n1", "agent": "direct_llm", "params": {"instruction": "旧计划"}}],
    }

    assert decode_plan(old_payload, []) is None


def test_cached_document_ids_are_rebound_for_current_request():
    nodes = [
        TaskNode(
            id="read",
            name="读取成绩",
            agent="office_doc",
            params={
                "doc_id": "doc-secret",
                "mode": "read",
                "instruction": "读取 doc_id=doc-secret",
            },
        )
    ]
    payload = encode_plan(nodes, _docs(), "读取文件")
    raw = json.dumps(payload, ensure_ascii=False)
    assert "doc-secret" not in raw
    assert "{{office_doc:0}}" in raw

    decoded = decode_plan(payload, _docs("new-doc-id"))
    assert decoded is not None
    rebound, _ = decoded
    assert rebound[0].params["doc_id"] == "new-doc-id"
    assert "new-doc-id" in rebound[0].params["instruction"]
    assert rebound[0].id != "read"


def test_plan_with_internal_server_path_is_not_cached():
    nodes = [
        TaskNode(
            id="n1",
            agent="atomic_step",
            params={"instruction": "读取", "path": "/app/private/data.txt"},
        )
    ]
    assert encode_plan(nodes, None, None) is None


def test_missing_document_binding_invalidates_cached_plan():
    node = TaskNode(
        id="n1",
        agent="office_doc",
        params={"doc_id": "doc-secret", "mode": "read", "instruction": "读取"},
    )
    payload = encode_plan([node], _docs(), None)
    assert decode_plan(payload, []) is None


def test_duplicate_filenames_disable_plan_cache():
    docs = [
        {"doc_id": "d1", "filename": "report.csv"},
        {"doc_id": "d2", "filename": "report.csv"},
    ]
    node = TaskNode(id="n1", agent="office_doc", params={"doc_id": "d1"})
    assert encode_plan([node], docs, None) is None


def test_secret_bearing_plan_is_not_cached():
    node = TaskNode(id="n1", agent="atomic_step", params={"api_key": "secret"})
    assert encode_plan([node], None, None) is None
