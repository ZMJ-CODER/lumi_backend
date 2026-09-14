"""E/F/G 项回归：契约异常可观测、插件准入白名单、模块拆分与多端流。

* E：契约适配失败不能静默降级 —— 告警 + 计数 + 结果上带审计标记；
* F：插件命名空间/版本准入校验（不受信只记录不阻断）；
* G：routing 三对象拆模块后旧导入路径仍可用；多端 SSE 帧同样带 version/seq。
"""

from __future__ import annotations

import json

import pytest
from lumi_contracts import ExecutionRequest, RouteDecision, RouteMode, TaskProfile
from lumi_contracts.routing.execution_request import ExecutionRequest as SplitExecutionRequest
from lumi_contracts.routing.route_decision import RouteDecision as SplitRouteDecision
from lumi_contracts.routing.task_profile import TaskProfile as SplitTaskProfile

from app.contracts.tools import (
    plugin_namespace_for,
    record_plugin_report,
    tool_spec_report,
    validate_plugin_declaration,
)


# ── E：契约异常可观测 ────────────────────────────────────────────


def test_contract_violation_is_reported_and_marked(monkeypatch):
    from lumi_contracts.adapters.legacy import UnsupportedContractVersion

    import app.contracts as contracts
    from app.services import tool_output_pipeline as pipeline

    def boom(*_args, **_kwargs):
        raise UnsupportedContractVersion("未知 content_type：weird")

    monkeypatch.setattr(contracts, "execution_result_from_envelope", boom)
    before = pipeline.contract_violation_report().get("UNSUPPORTED_CONTRACT_VERSION", 0)
    result = pipeline.normalize_execution_envelope(
        {"status": "success", "content_type": "weird", "data": {"a": 1}}
    )
    # 结果不丢（回退旧归一）
    assert result.status == "success"
    # 但必须留下可观测痕迹：计数 + 结果上的审计标记
    after = pipeline.contract_violation_report()["UNSUPPORTED_CONTRACT_VERSION"]
    assert after == before + 1
    marker = result.meta.quality_hints["contract_violation"]
    assert marker["code"] == "UNSUPPORTED_CONTRACT_VERSION"
    assert marker["fallback"] == "legacy_normalizer"
    assert marker["count"] == after


def test_contract_shaped_dict_is_not_mistaken_for_empty():
    """契约形态 dict（status+payload）不能因为读不到 data 被判成 empty。"""
    from app.services.tool_output_pipeline import normalize_execution_envelope

    result = normalize_execution_envelope(
        {"status": "success", "content_type": "structured", "payload": {"entries": [1, 2]}}
    )
    assert result.status == "success"
    assert result.data == {"entries": [1, 2]}


# ── F：插件准入白名单 ────────────────────────────────────────────


def test_plugin_namespace_is_derived_from_origin():
    assert plugin_namespace_for("plugins.tools.core.filesystem") == "lumi"
    assert plugin_namespace_for("plugins.workflows.developer.git") == "lumi"
    assert plugin_namespace_for("plugins.user.user_workflow_x") == "lumi_skill"
    assert plugin_namespace_for("plugins.desktop.send_email") == "lumi_client"
    # 显式声明优先
    assert plugin_namespace_for("plugins.tools.core.filesystem", declared="acme") == "acme"


def test_plugin_declaration_checks_namespace_and_version():
    class _T:
        version = "1.2.3"

    assert validate_plugin_declaration(_T(), namespace="lumi") == []
    assert validate_plugin_declaration(_T(), namespace="lumi_client") == []

    problems = validate_plugin_declaration(_T(), namespace="acme_corp")
    assert any("白名单" in item for item in problems)

    class _Bad:
        version = "v1"

    bad = validate_plugin_declaration(_Bad(), namespace="lumi")
    assert any("版本号" in item for item in bad)


def test_plugin_report_is_visible_without_blocking():
    record_plugin_report("demo_plugin", ["命名空间 'acme' 不在受信白名单内"])
    report = tool_spec_report()
    assert report["plugin_problems"]["demo_plugin"]


# ── G：模块拆分与多端流 ──────────────────────────────────────────


def test_routing_objects_are_split_but_still_re_exported():
    assert SplitTaskProfile is TaskProfile
    assert SplitRouteDecision is RouteDecision
    assert SplitExecutionRequest is ExecutionRequest
    request = ExecutionRequest(
        instruction="x",
        route=RouteDecision(mode=RouteMode.ATOMIC_READ),
    )
    assert request.route is not None and request.route.mode is RouteMode.ATOMIC_READ


@pytest.mark.asyncio
async def test_conversation_stream_frames_carry_version_and_seq(monkeypatch):
    """多端推送的 SSE 帧必须带契约版本与单调序号。"""
    from app.api.v1 import conversations

    uid = "11111111-1111-1111-1111-111111111111"
    published = [
        {"type": "message", "user_id": uid, "conversation_id": "c1", "content": "hi"},
        {"type": "title", "user_id": uid, "title": "t"},
        {"type": "message", "user_id": "other", "content": "ignored"},
    ]

    class _PubSub:
        def __init__(self):
            self._rows = [{"type": "message", "data": json.dumps(row)} for row in published]

        async def subscribe(self, _channel):
            return None

        async def listen(self):
            for row in self._rows:
                yield row

        async def unsubscribe(self, _channel):
            return None

        async def aclose(self):
            return None

    class _Redis:
        def pubsub(self):
            return _PubSub()

    class _Request:
        async def is_disconnected(self):
            return False

    monkeypatch.setattr(conversations, "get_redis", lambda: _Redis())

    response = await conversations.conversation_stream(request=_Request(), payload={"sub": uid})
    chunks = [chunk async for chunk in response.body_iterator]
    frames = [chunk for chunk in chunks if "event: message" in chunk or "event: title" in chunk]
    assert len(frames) == 2  # 其他用户的事件被过滤
    payloads = [json.loads(chunk.split("data: ", 1)[1].strip()) for chunk in frames]
    assert [item["seq"] for item in payloads] == [1, 2]
    assert all(item["version"] == 1 for item in payloads)
    assert payloads[0]["content"] == "hi"
