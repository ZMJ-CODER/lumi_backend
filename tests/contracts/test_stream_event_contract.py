"""阶段四回归：SSE 事件的契约版本与序号。

约束：
* 每一帧都带 ``version`` 与**流内单调递增**的 ``seq``；
* 编码**无损**：原有扁平字段一个不改、一个不少（前端形状不变）；
* 未知事件类型不抛错（前端可忽略，后端不失败）；
* 不可 JSON 序列化的元数据不得让整条流中断。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.contracts.events import STREAM_EVENT_VERSION, SseEventEncoder, encode_sse


def _payload(line: str) -> dict:
    assert line.startswith("data: ") and line.endswith("\n\n")
    return json.loads(line[len("data: "):].strip())


def test_frame_adds_version_and_seq_without_losing_fields():
    encoder = SseEventEncoder()
    frame = encoder.frame({"type": "delta", "content": "你好", "message_id": "m1"})
    assert frame["type"] == "delta"
    assert frame["version"] == STREAM_EVENT_VERSION
    assert frame["seq"] == 1
    # 原字段原样保留（前端按 type 分派的扁平结构不变）
    assert frame["content"] == "你好"
    assert frame["message_id"] == "m1"


def test_seq_is_monotonic_per_stream_and_independent_between_streams():
    first = SseEventEncoder()
    second = SseEventEncoder()
    assert [first.frame({"type": "delta"})["seq"] for _ in range(3)] == [1, 2, 3]
    assert first.last_seq == 3
    # 另一条流从 1 重新开始，互不影响
    assert second.frame({"type": "delta"})["seq"] == 1


def test_encoder_hoists_correlation_ids_and_keeps_explicit_job_id():
    encoder = SseEventEncoder(job_id="job-1", conversation_id="conv-1")
    frame = encoder.frame({"type": "step_started", "step_id": "s1"})
    assert frame["job_id"] == "job-1"
    assert frame["conversation_id"] == "conv-1"
    assert frame["step_id"] == "s1"
    # 事件自带的 job_id 优先，且不会出现重复键
    other = encoder.frame({"type": "delta", "job_id": "job-2"})
    assert other["job_id"] == "job-2"


def test_empty_job_id_is_not_dropped_from_the_frame():
    encoder = SseEventEncoder()
    frame = encoder.frame({"type": "delta", "job_id": "", "content": "x"})
    assert "job_id" in frame and frame["job_id"] == ""


def test_unknown_event_type_is_forwarded_not_rejected():
    encoder = SseEventEncoder()
    frame = encoder.frame({"type": "future_event_from_newer_backend", "payload": 1})
    assert frame["type"] == "future_event_from_newer_backend"
    assert frame["payload"] == 1


def test_missing_type_falls_back_to_error_frame():
    frame = SseEventEncoder().frame({"message": "boom"})
    assert frame["type"] == "error"
    assert frame["message"] == "boom"


def test_previous_version_and_seq_in_payload_do_not_break_monotonicity():
    encoder = SseEventEncoder()
    frame = encoder.frame({"type": "delta", "version": 99, "seq": 500, "content": "x"})
    # 契约字段由编码器决定：不能因为载荷里带了旧版本号就破坏单调性。
    assert frame["version"] == STREAM_EVENT_VERSION
    assert frame["seq"] == 1


def test_encode_is_json_safe_for_non_serializable_metadata():
    line = SseEventEncoder().encode(
        {"type": "done", "at": datetime(2024, 1, 1, tzinfo=timezone.utc)}
    )
    payload = _payload(line)
    assert payload["type"] == "done"
    assert payload["seq"] == 1
    assert isinstance(payload["at"], str)


def test_encode_sse_roundtrip():
    frame = {"type": "delta", "version": 1, "seq": 7, "content": "ok"}
    assert _payload(encode_sse(frame)) == frame
