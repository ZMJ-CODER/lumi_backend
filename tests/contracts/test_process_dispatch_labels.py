"""过程条目/事件里的**结构化能力标签**（方案《资源能力层》§七）回归。

这一块解决的是前端最后一处"只能靠工具名猜"的地方：过程日志与 SSE 步骤帧里给出
``capability`` / ``resource_type`` / ``provider_id`` / ``provider_name`` / ``display_name``，
前端据此显示"正在写入工作区"，而不是在界面主文案里出现 ``workspace_write``。

五条不变量：

1. **开关无关的目录事实**：能力/资源/Provider 来自统一能力目录（Phase 1 元数据），
   与任何 feature flag 无关；只有 ``display_name``（模型本轮看到的名字）受收敛开关影响；
2. **模型看到的名字必须是真的**：收敛关闭时 ``display_name`` 等于工具名（不谎报成 ``Write``），
   且等于工具名时不重复下发（事件里已有 ``tool_name``）；
3. **不猜**：认不出的工具、形状怪异的输入（带空格/斜杠/引号）一律返回空；
4. **绝不含用户数据**：标签走闭集形状闸门，参数/路径/正文不可能借道；
5. **老载荷逐字不变**：标签默认 ``None`` + ``exclude_none`` ⇒ 老条目的 JSON 一个键都不多。
"""

from __future__ import annotations

import json

import pytest

from lumi_contracts import ProcessLogEntry
from lumi_contracts.events.process import label_value, merge_process_log


@pytest.fixture()
def surface_off(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_SURFACE", False)
    return settings


@pytest.fixture()
def surface_on(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_SURFACE", True)
    return settings


# ── 1. 标签派生 ─────────────────────────────────────────────


def test_labels_are_catalog_facts_independent_of_flags(surface_off):
    from app.agents.capabilities.broker.resource_dispatch import dispatch_labels

    labels = dispatch_labels("workspace_write")
    assert labels == {
        "capability": "resource.write",
        "resource_type": "workspace",
        "provider_id": "lumi.local.workspace",
        "provider_name": "workspace_provider",
        # 缺口 2c：稳定展示维度（前端按它渲染，不按物理 id 分支）
        "provider_kind": "workspace",
        "provider_label": "工作区能力",
    }, "开关关闭时目录事实照样下发；display_name 等于工具名，故省略"


def test_display_name_appears_only_when_converged(surface_on):
    from app.agents.capabilities.broker.resource_dispatch import dispatch_labels

    labels = dispatch_labels("workspace_write")
    assert labels["display_name"] == "Write"
    assert labels["capability"] == "resource.write"


def test_office_and_memory_resources_are_labelled(surface_off):
    """办公文档能力**被正确路由**——不再死断言某个物理 provider_id。

    缺口 2c：物理 id 会在迁移期改名（``lumi.client.forwarder`` →
    ``lumi.client.office_document``），因此断言落在稳定维度上（能力/资源类型/逻辑
    Provider 名/展示字段）。旧 id 仍然被接受，由兼容期测试单独覆盖。
    """
    from app.agents.capabilities.broker.resource_dispatch import dispatch_labels

    office = dispatch_labels("office_doc_read")
    assert (office["capability"], office["resource_type"]) == ("resource.read", "office_document")
    assert office["provider_name"] == "office_document_provider"
    assert office["provider_kind"] == "office_document"
    assert office["provider_label"] == "办公文档能力"
    # 物理 id 仍然下发（排障用），但已经独立于通用转发 id
    assert office["provider_id"] == "lumi.client.office_document"

    # 写侧同样落到办公文档 Provider（不是"按名字猜"）
    edit = dispatch_labels("office_doc_edit")
    assert (edit["capability"], edit["provider_kind"]) == ("resource.write", "office_document")


def test_unknown_and_junk_tool_names_yield_nothing(surface_off):
    from app.agents.capabilities.broker.resource_dispatch import dispatch_labels

    assert dispatch_labels("totally_unknown_tool") == {}
    assert dispatch_labels("") == {}
    # 形状闸门：带斜杠/空格/引号的输入不可能借道带出参数
    assert dispatch_labels("workspace_write/../../etc/passwd") == {}
    assert dispatch_labels("workspace_write --path=/home/u/.env") == {}
    assert dispatch_labels("workspace_write'; DROP TABLE jobs;--") == {}


def test_label_value_is_a_shape_gate():
    assert label_value("resource.write") == "resource.write"
    assert label_value("lumi.local.workspace") == "lumi.local.workspace"
    assert label_value("workspace_write") == "workspace_write"
    for bad in ("/home/u/.env", "两个 词", "a b", 'x"y', "a/b", "x" * 81, ""):
        assert label_value(bad) is None, bad


# ── 2. 契约：载荷后向兼容 ───────────────────────────────────


def test_entry_payload_has_no_new_keys_when_labels_absent():
    entry = ProcessLogEntry(entry_id="step:s1", step_id="s1", tool_name="workspace_write")
    payload = entry.model_dump(mode="json", exclude_none=True)
    assert set(payload) == {
        "id", "entry_id", "kind", "title", "summary", "detail", "status",
        "step_id", "call_id", "tool_name", "sequence", "occurred_at", "job_id",
    }, "老条目一个键都不能多（前端按需读，不要求它认得空字段）"


def test_entry_carries_labels_when_present():
    entry = ProcessLogEntry(
        entry_id="step:s1",
        tool_name="workspace_write",
        capability="resource.write",
        resource_type="workspace",
        display_name="Write",
    )
    fields = entry.to_sse_fields()
    assert fields["capability"] == "resource.write"
    assert fields["resource_type"] == "workspace"
    assert fields["display_name"] == "Write"


def test_from_event_reads_labels_and_rejects_junk():
    entry = ProcessLogEntry.from_event({
        "type": "tool_completed",
        "tool_name": "workspace_write",
        "capability": "resource.write",
        "resource_type": "/home/u/.env",  # 形状不符 → 丢弃
    })
    assert entry.capability == "resource.write"
    assert entry.resource_type is None


def test_merge_keeps_labels_from_either_frame():
    """先到的 running 帧可能没带标签，后到的 completed 帧补上。"""
    running = ProcessLogEntry(entry_id="call:c1", call_id="c1", tool_name="workspace_write")
    completed = ProcessLogEntry(
        entry_id="call:c1", call_id="c1", tool_name="workspace_write",
        capability="resource.write", resource_type="workspace",
    )
    merged = merge_process_log([running], [completed])
    assert len(merged) == 1
    assert merged[0].capability == "resource.write"
    assert merged[0].resource_type == "workspace"


# ── 3. 过程日志与 SSE 步骤帧 ────────────────────────────────


def _step_payload(job: dict) -> dict:
    from app.contracts.process_log import process_log_from_job, process_log_payload

    return process_log_payload(process_log_from_job(job))[0]


def test_process_log_step_carries_labels(surface_off):
    payload = _step_payload({
        "job_id": "j1",
        "routing": {"steps": [{"id": "s1", "tool": "workspace_write", "status": "completed"}]},
        "nodes": [],
    })
    assert payload["tool_name"] == "workspace_write"
    assert payload["capability"] == "resource.write"
    assert payload["resource_type"] == "workspace"
    assert payload["provider_id"] == "lumi.local.workspace"
    assert "display_name" not in payload, "等于工具名时不重复下发"


def test_process_log_respects_explicit_display_name(surface_off):
    """调用方已记下"模型当时叫的名字"时以它为准（哪怕收敛开关现在是关的）。"""
    payload = _step_payload({
        "job_id": "j1",
        "routing": {"steps": [{
            "id": "s1", "tool": "workspace_write", "status": "completed", "display_name": "Write",
        }]},
        "nodes": [],
    })
    assert payload["display_name"] == "Write"


def test_process_log_unknown_tool_adds_no_labels(surface_off):
    payload = _step_payload({
        "job_id": "j1",
        "routing": {"steps": [{"id": "s1", "tool": "totally_unknown_tool", "status": "completed"}]},
        "nodes": [],
    })
    for key in (
        "capability",
        "resource_type",
        "provider_id",
        "provider_name",
        "provider_kind",
        "provider_label",
        "display_name",
    ):
        assert key not in payload, key


def test_step_frame_adapter_carries_labels(surface_off):
    """SSE 步骤帧（canonical）也要带标签：前端实时渲染与刷新后一致。"""
    from app.contracts.event_adapter import _step_payload as canonical_step_payload

    payload = canonical_step_payload({
        "type": "tool_completed",
        "tool_name": "office_doc_edit",
        "step_id": "s2",
        "status": "completed",
    })
    assert payload["tool_name"] == "office_doc_edit"
    assert payload["capability"] == "resource.write"
    assert payload["resource_type"] == "office_document"


def test_labels_never_carry_user_input(surface_off):
    """**安全断言**：步骤参数里的路径/正文不会借标签漏出去。"""
    step = {
        "id": "s1",
        "tool": "workspace_write",
        "status": "completed",
        "params": {"path": "/home/secret/.env", "content": "TOP-SECRET-TOKEN"},
        "result": {"output": "/home/secret/.env TOP-SECRET-TOKEN"},
    }
    payload = _step_payload({"job_id": "j1", "routing": {"steps": [step]}, "nodes": []})
    blob = json.dumps(payload, ensure_ascii=False)
    for label_key in (
        "capability",
        "resource_type",
        "provider_id",
        "provider_name",
        "provider_kind",
        "provider_label",
        "display_name",
    ):
        value = payload.get(label_key)
        if value is None:
            continue
        assert "secret" not in value and "TOKEN" not in value and "/" not in value, (label_key, value)
    assert "TOP-SECRET-TOKEN" not in json.dumps(
        {
            key: payload.get(key)
            for key in (
                "capability",
                "resource_type",
                "provider_id",
                "provider_name",
                "provider_kind",
                "provider_label",
            )
        },
        ensure_ascii=False,
    )
    del blob


# ── 4. 预览 ≠ 现状（这条区分是本次改动引入的）────────────────


def test_preview_still_shows_converged_name_when_flag_off(surface_off):
    """管理端"模型可见面"卡片是**预览**：开关关闭时也要能算出收敛后的名字。"""
    from app.agents.capabilities.views.resource_surface import (
        display_name_for,
        hidden_tools,
        model_tool_for,
    )

    # 预览：不受开关影响
    assert model_tool_for("resource.write", tool="workspace_write", resource_type="workspace") == "Write"
    assert "workspace_write" in hidden_tools(["workspace_write"])
    # 现状：模型本轮看到的就是工具名
    assert display_name_for("workspace_write") == "workspace_write"
