"""统一 ResultStore 与引用体系回归（方案 §1）。

覆盖验收场景：

* 短结果按分层策略进小 KV（**不上传 Blob**）；
* 长正文（如几十页 PPT 正文）落 Blob，Job 只留最小引用，正文可按引用读取；
* 引用过期 → ``RESULT_REF_EXPIRED``（明确错误，不静默当空结果）；
* ``sha256`` 不符 → 完整性校验失败，拒绝交付；
* 跨用户读取 → ``RESULT_REF_FORBIDDEN``；
* 按预算加载：只读摘要 / 只读字段 / 分页 / 字符预算截断；
* 历史数据按**存储时**的 ``schema_version`` 解析，失败降级为原始内容（不硬解析）。
"""

from __future__ import annotations

import asyncio

import pytest

from lumi_contracts.persistence.result_store import (
    RESULT_REF_EXPIRED,
    RESULT_REF_FORBIDDEN,
    RESULT_REF_INTEGRITY_FAILED,
    RESULT_REF_UNAVAILABLE,
    LoadBudget,
    ResultRefError,
    ResultStorageKind,
    TieringPolicy,
)
from app.services import result_store as store_module
from app.services.result_store import (
    LocalBlobPort,
    ResultStore,
    SaveResultRequest,
)


class _MemoryKv:
    """最小 KV 替身（不依赖 Redis）。"""

    def __init__(self) -> None:
        self.items: dict[str, str] = {}

    async def put(self, key: str, payload: str, *, ttl_seconds: int) -> None:
        self.items[key] = payload

    async def get(self, key: str) -> str | None:
        return self.items.get(key)

    async def delete(self, key: str) -> None:
        self.items.pop(key, None)

    async def find_by_result_id(self, result_id: str) -> str | None:
        suffix = f":{result_id}"
        return next((value for key, value in self.items.items() if key.endswith(suffix)), None)


@pytest.fixture()
def store():
    """在受控工作目录（``.ptmp``）里起一个 ResultStore，避免系统临时目录权限问题。"""
    import shutil
    import uuid
    from pathlib import Path

    root = Path(".ptmp") / f"result-store-{uuid.uuid4().hex[:8]}"
    kv = _MemoryKv()
    local = LocalBlobPort(root / "blobs")
    instance = ResultStore(
        kv=kv,
        local_blob=local,
        blob=local,
        tiering=TieringPolicy(redis_max_bytes=200, local_max_bytes=400, blob_enabled=True),
        default_ttl_seconds=3600,
    )
    try:
        yield instance, kv, root / "blobs"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_short_result_stays_in_kv_and_never_touches_blob(store):
    """验收：普通短回答/短结果不得被错误上传 Blob。"""
    instance, kv, blob_root = store
    receipt = asyncio.run(
        instance.save(
            SaveResultRequest(
                result={"success": True, "content": "短回答", "count": 2},
                user_id="u1",
                job_id="j1",
                step_id="s1",
            )
        )
    )
    assert receipt is not None
    assert receipt.storage_kind == ResultStorageKind.REDIS.value
    assert receipt.minimal_ref.keys() == {"id", "sha256"}
    assert not blob_root.exists() or not any(blob_root.rglob("*"))
    body = asyncio.run(instance.load_body(receipt.minimal_ref, user_id="u1"))
    assert body["content"] == "短回答"


def test_large_result_goes_to_blob_and_is_readable_by_reference(store):
    """验收：几十页正文不进 Job 快照，但**可以按引用完整取回**。"""
    instance, kv, blob_root = store
    pages = [{"index": index, "text": "第%d页正文" % index + "x" * 400} for index in range(40)]
    receipt = asyncio.run(
        instance.save(
            SaveResultRequest(
                result={"success": True, "items": pages, "content": "PPT 正文"},
                user_id="u2",
                job_id="j2",
                step_id="s2",
            )
        )
    )
    assert receipt is not None
    assert receipt.storage_kind == ResultStorageKind.BLOB.value
    assert any(blob_root.rglob("*.json")), "大正文必须真的落到 Blob 后端"
    # 元数据仍在 KV（读取先拿元数据再取正文）。
    assert any(str(key).endswith(receipt.ref.id) for key in kv.items)
    loaded = asyncio.run(instance.load(receipt.minimal_ref, user_id="u2"))
    assert len(loaded.body["items"]) == 40


def test_expired_reference_raises_explicit_error(store):
    """验收：结果引用过期必须明确报错，不显示"空成功"。"""
    instance, _kv, _root = store
    receipt = asyncio.run(
        instance.save(
            SaveResultRequest(
                result={"success": True, "content": "会过期"},
                user_id="u3",
                job_id="j3",
                ttl_seconds=1,
            )
        )
    )
    assert receipt is not None
    instance._clock = lambda: receipt.ref.created_at + 5  # noqa: SLF001 - 用可控时钟模拟过期
    with pytest.raises(ResultRefError) as excinfo:
        asyncio.run(instance.load(receipt.minimal_ref, user_id="u3"))
    assert excinfo.value.code == RESULT_REF_EXPIRED
    assert asyncio.run(instance.load_body(receipt.minimal_ref, user_id="u3")) is None


def test_integrity_failure_is_rejected(store):
    """验收：引用 hash 错误必须被拒绝（不得把篡改后的正文交给下游）。"""
    instance, kv, _root = store
    receipt = asyncio.run(
        instance.save(
            SaveResultRequest(result={"success": True, "content": "原始"}, user_id="u4", job_id="j4")
        )
    )
    assert receipt is not None
    key = next(key for key in kv.items if str(key).endswith(receipt.ref.id))
    kv.items[key] = kv.items[key].replace("原始", "篡改")
    with pytest.raises(ResultRefError) as excinfo:
        asyncio.run(instance.load(receipt.minimal_ref, user_id="u4"))
    assert excinfo.value.code == RESULT_REF_INTEGRITY_FAILED


def test_cross_owner_read_is_forbidden(store):
    """验收：引用按 owner 隔离（跨用户读取一律拒绝）。"""
    instance, _kv, _root = store
    receipt = asyncio.run(
        instance.save(
            SaveResultRequest(result={"success": True, "content": "私密"}, user_id="owner-a", job_id="j5")
        )
    )
    assert receipt is not None
    with pytest.raises(ResultRefError) as excinfo:
        asyncio.run(instance.load(receipt.minimal_ref, user_id="owner-b"))
    assert excinfo.value.code == RESULT_REF_FORBIDDEN
    assert asyncio.run(instance.load_body(receipt.minimal_ref, user_id="owner-b")) is None


def test_missing_record_reports_unavailable(store):
    instance, _kv, _root = store
    with pytest.raises(ResultRefError) as excinfo:
        asyncio.run(instance.load({"id": "nope", "sha256": "0" * 64}, user_id="u1"))
    assert excinfo.value.code == RESULT_REF_UNAVAILABLE


def test_budgeted_load_supports_summary_fields_paging_and_chars(store):
    """验收：只读摘要 / 只读指定字段 / 分页 / 字符预算截断。"""
    instance, _kv, _root = store
    receipt = asyncio.run(
        instance.save(
            SaveResultRequest(
                result={
                    "success": True,
                    "summary": "一句话摘要",
                    "content": "正文" * 500,
                    "items": [{"index": index} for index in range(30)],
                },
                user_id="u6",
                job_id="j6",
            )
        )
    )
    assert receipt is not None

    summary_only = asyncio.run(
        instance.load_bounded(
            receipt.minimal_ref, user_id="u6", budget=LoadBudget(summary_only=True)
        )
    )[1]
    assert set(summary_only.body) <= {"summary", "status", "count"}
    assert "content" not in summary_only.body

    fields = asyncio.run(
        instance.load_bounded(
            receipt.minimal_ref, user_id="u6", budget=LoadBudget(fields=("items",))
        )
    )[1]
    assert set(fields.body) == {"items"}

    paged = asyncio.run(
        instance.load_bounded(
            receipt.minimal_ref,
            user_id="u6",
            budget=LoadBudget(fields=("items",), page_size=10, page_offset=10),
        )
    )[1]
    assert len(paged.body["items"]) == 10
    assert paged.body["items"][0]["index"] == 10
    assert paged.next_offset == 20
    assert paged.truncated is True

    bounded = asyncio.run(
        instance.load_bounded(
            receipt.minimal_ref, user_id="u6", budget=LoadBudget(fields=("content",), max_chars=100)
        )
    )[1]
    assert bounded.truncated is True
    assert "content" in bounded.truncated_fields
    assert len(bounded.body["content"]) <= 100 + len("…[已按预算截断]")


def test_history_is_parsed_with_the_stored_schema_version(store):
    """验收：旧 result_ref 用旧 schema 解析；不兼容时降级展示而不是硬解析。"""
    instance, _kv, _root = store
    receipt = asyncio.run(
        instance.save(
            SaveResultRequest(
                result={"success": True, "content": "v1 结果"},
                user_id="u7",
                job_id="j7",
                schema_name="lumi.execution_result",
                schema_version=1,
            )
        )
    )
    assert receipt is not None
    resolved = asyncio.run(instance.load(receipt.minimal_ref, user_id="u7"))
    assert resolved.ok and resolved.degraded is False
    assert resolved.ref.schema_version == 1

    future = asyncio.run(
        instance.save(
            SaveResultRequest(
                result={"success": True, "content": "未来版本"},
                user_id="u7",
                job_id="j7",
                schema_name="lumi.execution_result",
                schema_version=99,
            )
        )
    )
    assert future is not None
    degraded = asyncio.run(instance.load(future.minimal_ref, user_id="u7"))
    assert degraded.degraded is True
    assert degraded.degradation == "schema_mismatch"
    assert degraded.body  # 仍能展示原始内容（不是空结果）


def test_tiering_policy_prefers_blob_only_when_available():
    policy = TieringPolicy(redis_max_bytes=10, local_max_bytes=20, blob_enabled=True, blob_fallback_local=False)
    assert policy.decide(5) == ResultStorageKind.REDIS
    assert policy.decide(15) == ResultStorageKind.LOCAL
    assert policy.decide(50, blob_available=True) == ResultStorageKind.BLOB
    assert policy.decide(50, blob_available=False) == ResultStorageKind.LOCAL


def test_local_blob_path_rejects_traversal():
    from pathlib import Path

    root = Path(".ptmp") / "result-store-traversal"
    with pytest.raises(ValueError):
        store_module.local_blob_path(root, "../escape.json")
    assert store_module.local_blob_path(root, "owner/r.json").name == "r.json"


def test_result_storage_key_is_owner_scoped():
    a = store_module.result_storage_key("user-a", "r1")
    b = store_module.result_storage_key("user-b", "r1")
    assert a != b
    assert a.endswith(":r1") and b.endswith(":r1")
