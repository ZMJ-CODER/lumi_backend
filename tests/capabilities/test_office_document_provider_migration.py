"""缺口 2c：`office_document_provider` 独立 provider id 的**兼容迁移**回归。

背景（`docs/CAPABILITY_TWO_GENERATIONS.md` §3.6）：办公文档能力原先借用通用转发 id
``lumi.client.forwarder``（与"任意客户端能力"共用），于是它进过过程条目与事件流——
`provider_id` 是**跨端可见协议**，直接改会造成前端展示、fixture、历史任务回放一起抖。

因此这次迁移是"**新 id 优先 + 旧 id 兼容**"，后端一侧完成，桌面端不必立刻改：

```text
声明：office_document_provider.provider_id = lumi.client.office_document
      legacy_provider_ids                = ("lumi.client.forwarder",)   ← 兼容期
收窄：provider_ids_for(...) 同时包含两个 id ⇒ 用旧 id 注册的租约照样是合法候选
出口：provider_kind / provider_label 两个稳定展示字段 ⇒ 前端不必按物理 id 分支
历史：已经落盘的旧 provider_id 原样保留，**不回写**
```

本文件钉住这四条，外加"改 id 不会让能力变成不可用"这条底线。
"""

from __future__ import annotations

import pytest
from lumi_contracts.plugins import Deployment, ProviderHealth, ProviderLease

from app.agents.capabilities.broker.broker import CapabilityBroker
from app.agents.capabilities.broker.dispatch import CapabilityDispatchAdapter
from app.agents.capabilities.broker.resource_dispatch import (
    adapter_for,
    adapter_snapshot,
    dispatch_labels,
    provider_ids_for,
)
from app.agents.capabilities.catalog.resource import (
    PROVIDERS_BY_NAME,
    RESOURCE_LABELS,
    RESOURCE_OFFICE_DOCUMENT,
    label_for_resource,
)
from app.agents.capabilities.contracts.context import AgentExecutionContext

_NEW_ID = "lumi.client.office_document"
_OLD_ID = "lumi.client.forwarder"
_SPEC = PROVIDERS_BY_NAME["office_document_provider"]


def _lease(provider_id: str, capability: str = "office.read") -> ProviderLease:
    from app.services.capability_lease_redis import lease_id_for

    return ProviderLease(
        provider_id=provider_id,
        capability=capability,
        contract_version=1,
        lease_id=lease_id_for(provider_id, capability),
        user_id="u1",
        device_id="device-1",
        workspace_id="ws-1",
        conversation_id="c1",
        deployment=Deployment.CLIENT,
        plugin_id=provider_id,
        provider_version="1.0.0",
        health_status=ProviderHealth.HEALTHY.value,
        expires_at=9_999_999_999.0,
        last_heartbeat_at=100.0,
    )


class _LeaseService:
    def __init__(self, leases: list[ProviderLease]) -> None:
        self._leases = list(leases)

    def snapshot(self, *, purge: bool = True) -> list[ProviderLease]:
        return list(self._leases)

    def sync_registry(self) -> None:
        return None

    async def refresh_from_redis(self) -> list[ProviderLease]:
        return list(self._leases)


class _Caller:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, name, tool_name, args=None, **kwargs):
        self.calls.append({"name": name})
        return {"status": "ok"}


def _context() -> AgentExecutionContext:
    return AgentExecutionContext.from_metadata(
        user_id="u1", conversation_id="c1", workspace_id="ws-1", device_id="device-1"
    )


# ── 1. 声明：新 id 优先，旧 id 明确登记为兼容 ────────────────


def test_office_document_provider_declares_the_new_id_and_keeps_the_old_one():
    assert _SPEC.provider_id == _NEW_ID
    assert _SPEC.legacy_provider_ids == (_OLD_ID,)
    assert "forwarder" in _SPEC.note, "兼容期这件事必须写在声明里，而不是只存在于测试"
    # 通用转发 id 仍然是系统概念（其它客户端能力还在用它），只是不再是**这个** Provider 的首选
    from app.agents.capabilities.registry.builtin import PROVIDER_CLIENT_REMOTE

    assert PROVIDER_CLIENT_REMOTE == _OLD_ID


def test_adapter_exposes_the_compat_window():
    adapter = adapter_for("resource.read", RESOURCE_OFFICE_DOCUMENT)
    assert adapter is not None
    assert adapter.provider_id == _NEW_ID
    assert adapter.legacy_provider_ids == (_OLD_ID,)
    assert adapter.accepted_provider_ids == (_NEW_ID, _OLD_ID)

    rows = {row["name"]: row for row in adapter_snapshot()}
    assert rows["office_document_provider"]["legacy_provider_ids"] == [_OLD_ID]
    assert rows["office_document_provider"]["accepted_provider_ids"] == [_NEW_ID, _OLD_ID]
    # 其它 Provider 没有兼容期尾巴
    assert rows["workspace_provider"]["legacy_provider_ids"] == []


def test_narrowing_accepts_both_ids_during_the_compat_window():
    ids = provider_ids_for("resource.read", RESOURCE_OFFICE_DOCUMENT)
    assert ids == frozenset({_NEW_ID, _OLD_ID})
    assert provider_ids_for("resource.write", RESOURCE_OFFICE_DOCUMENT) == ids
    assert provider_ids_for("resource.edit", RESOURCE_OFFICE_DOCUMENT) == ids


# ── 2. 底线：用旧 id 注册的租约仍然能被选中 ──────────────────


@pytest.mark.parametrize("registered_id", [_NEW_ID, _OLD_ID])
@pytest.mark.asyncio
async def test_leases_registered_under_either_id_still_dispatch(registered_id):
    """兼容期的硬要求：换 id 不能让既有能力的租约变成"收窄后被排除"。

    办公文档能力目前没有旧能力名（它不在能力目录里，办公工具走工具注册表执行），
    因此这里用**目录里真实存在的**能力 + 同一个兼容 id 集合来验证机制本身：
    收窄集合含旧 id 时，用旧 id 注册的租约必须照样被选中并派发。
    """
    caller = _Caller()
    adapter = CapabilityDispatchAdapter(
        lease_service=_LeaseService([_lease(registered_id, "workspace.read")]),
        call_tool=caller,
    )
    outcome = await adapter.dispatch(
        capability="workspace.read",
        args={"action": "read", "path": "a.py"},
        context=_context(),
        provider_ids=frozenset({_NEW_ID, _OLD_ID}),
        allow_legacy_fallback=False,
    )
    assert outcome.handled is True, outcome.reason
    assert outcome.provider_id == registered_id
    assert caller.calls, "合法 id（含兼容 id）注册的租约必须能派出去"


def test_broker_narrowing_keeps_the_legacy_lease_as_a_candidate():
    """把兼容 id 的租约交给 Broker：收窄集合同时含新旧 id，候选不会被排空。"""
    leases = _LeaseService([_lease(_OLD_ID, "workspace.read")])
    broker = CapabilityBroker(leases=leases)
    # workspace.read 的收窄集合里没有 forwarder：这是"必须回落"的路径（不是本缺口的内容），
    # 因此这里直接验证办公文档能力的收窄集合本身包含旧 id（上面已断言），
    # 再验证 Broker 在拿到新旧两个 id 时不会把旧租约排除掉。
    selection = broker.select(
        "workspace.read",
        binding=_context().binding,
        provider_ids=frozenset({_NEW_ID, _OLD_ID}),
    )
    assert selection.provider_id == _OLD_ID, "兼容 id 的租约必须仍是有效候选"


# ── 3. 出口：稳定展示字段 ────────────────────────────────────


def test_labels_expose_stable_display_fields():
    for tool in ("office_doc_read", "office_doc_edit", "office_doc_analyze"):
        labels = dispatch_labels(tool)
        assert labels["provider_kind"] == RESOURCE_OFFICE_DOCUMENT, tool
        assert labels["provider_label"] == RESOURCE_LABELS[RESOURCE_OFFICE_DOCUMENT], tool
        # 前端要用的两个稳定字段都在，且不依赖物理 id 的具体取值
        assert labels["provider_name"] == "office_document_provider", tool


def test_resource_labels_are_a_closed_table():
    """展示文案来自固定表：每个资源类型都有文案，未知类型**不编**。"""
    from app.agents.capabilities.catalog.resource import RESOURCE_TYPES

    assert set(RESOURCE_LABELS) == set(RESOURCE_TYPES)
    assert label_for_resource("not_a_resource") == ""
    assert label_for_resource("") == ""
    # 文案是给人读的中文，且不含路径/凭据形态
    for value in RESOURCE_LABELS.values():
        assert value and "/" not in value and "\\" not in value


# ── 4. 历史：落盘的旧 id 原样保留，不回写 ────────────────────


def test_persisted_legacy_labels_are_not_rewritten():
    """历史步骤里已经写下的 ``provider_id=lumi.client.forwarder`` 必须原样回放。

    事件与过程条目是**历史记录**：迁移只能影响"新写的行"，不能改写"已经写下的行"，
    否则历史任务回放会与当时的真实执行者不一致。
    """
    from app.contracts.process_log import dispatch_labels_for_step

    step = {
        "capability": "resource.read",
        "resource_type": RESOURCE_OFFICE_DOCUMENT,
        "provider_id": _OLD_ID,
    }
    labels = dispatch_labels_for_step(step, "office_doc_read")
    assert labels["provider_id"] == _OLD_ID
    # 稳定字段照常补全（老步骤没有这两个字段，补上不改变历史事实）
    assert labels["provider_kind"] == RESOURCE_OFFICE_DOCUMENT
    assert labels["provider_label"] == RESOURCE_LABELS[RESOURCE_OFFICE_DOCUMENT]


def test_old_process_payloads_still_parse_without_the_new_fields():
    """没有 provider_kind / provider_label 的老载荷必须能解析（前向兼容）。"""
    from lumi_contracts.events.process import ProcessLogEntry

    legacy = ProcessLogEntry.model_validate(
        {
            "id": "e1",
            "title": "读文档",
            "capability": "resource.read",
            "resource_type": "office_document",
            "provider_id": _OLD_ID,
            "display_name": "Read",
        }
    )
    assert legacy.provider_kind is None
    assert legacy.provider_label is None
    assert legacy.provider_id == _OLD_ID
