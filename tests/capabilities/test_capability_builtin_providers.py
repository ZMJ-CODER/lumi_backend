"""阶段 1 收尾回归：内置 Provider 把现有能力接进统一契约（行为不变）。

要点：

1. 5 个内置能力都有 Provider，且位置与 ``data_locality`` 自洽
   （``artifact.create`` 在服务端；本地能力在客户端侧声明）；
2. 本地能力在服务端**明确拒绝就地执行**，绝不允许"客户端不在线就在服务端跑"；
3. ``artifact.create`` 的服务端实现复用既有渲染器，产物名走返回值而不是暴露绝对路径；
4. Provider 忽略调用方传的工作区/项目（授权只来自上下文）；
5. 注册是幂等的覆盖（不会留下幽灵路由）。
"""

from __future__ import annotations

import asyncio

from lumi_contracts.plugins import (
    CapabilityErrorCode,
    CapabilityInvocation,
    Deployment,
)

from app.agents.capabilities import (
    CAPABILITY_ARTIFACT_CREATE,
    CAPABILITY_CODE_EXECUTE,
    CAPABILITY_GIT_OPERATIONS,
    CAPABILITY_WORKSPACE_READ,
    CAPABILITY_WORKSPACE_WRITE,
    AgentExecutionContext,
    CapabilityRegistry,
    register_builtin_providers,
)
from app.agents.capabilities.registry.builtin import (
    PROVIDER_CLIENT_CODE,
    PROVIDER_CLIENT_GIT,
    PROVIDER_CLIENT_WORKSPACE,
    PROVIDER_SERVER_ARTIFACT,
    ServerArtifactProvider,
    builtin_providers,
    capability_requires_client,
)


from _paths import REPO_ROOT
def _context(**overrides) -> AgentExecutionContext:
    payload = {
        "user_id": "u1",
        "conversation_id": "c1",
        "workspace_id": "ws-1",
        "device_id": "device-1",
        "project_ids": ["proj-1"],
    }
    payload.update(overrides)
    return AgentExecutionContext.from_metadata(**payload)


def _invoke(provider, capability: str, arguments: dict, *, context=None):
    return asyncio.run(
        provider.invoke(
            CapabilityInvocation(
                capability=capability, arguments=arguments, request_id="r1"
            ),
            context=context or _context(),
        )
    )


def test_all_builtin_capabilities_have_providers_on_the_right_side():
    registry = CapabilityRegistry()
    register_builtin_providers(registry=registry)
    assert [item.provider_id for item in registry.registrations(CAPABILITY_ARTIFACT_CREATE)] == [
        PROVIDER_SERVER_ARTIFACT
    ]
    assert [item.provider_id for item in registry.registrations(CAPABILITY_WORKSPACE_READ)] == [
        PROVIDER_CLIENT_WORKSPACE
    ]
    assert [item.provider_id for item in registry.registrations(CAPABILITY_WORKSPACE_WRITE)] == [
        PROVIDER_CLIENT_WORKSPACE
    ]
    assert [item.provider_id for item in registry.registrations(CAPABILITY_CODE_EXECUTE)] == [
        PROVIDER_CLIENT_CODE
    ]
    assert [item.provider_id for item in registry.registrations(CAPABILITY_GIT_OPERATIONS)] == [
        PROVIDER_CLIENT_GIT
    ]
    # 位置与本地性自洽：本地能力必须在客户端侧声明。
    for name in (CAPABILITY_WORKSPACE_READ, CAPABILITY_CODE_EXECUTE, CAPABILITY_GIT_OPERATIONS):
        assert registry.registrations(name)[0].deployment is Deployment.CLIENT
    assert registry.registrations(CAPABILITY_ARTIFACT_CREATE)[0].deployment is Deployment.SERVER


def test_local_capabilities_refuse_server_side_inline_execution():
    """服务端不得就地执行本地能力（客户端离线 ≠ 可以在云端跑本地读取）。"""
    provider = next(
        item for item in builtin_providers() if item.provider_id == PROVIDER_CLIENT_WORKSPACE
    )
    result = _invoke(provider, CAPABILITY_WORKSPACE_READ, {"action": "read", "path": "a.py"})
    assert result.ok is False
    assert result.error_code == CapabilityErrorCode.PROVIDER_OFFLINE.value
    assert result.retryable is True  # 客户端回来就能重试
    assert capability_requires_client(CAPABILITY_WORKSPACE_READ) is True
    assert capability_requires_client(CAPABILITY_ARTIFACT_CREATE) is False


def test_artifact_provider_uses_server_renderer_and_reports_artifact_name(monkeypatch):
    """服务端产物能力复用既有渲染器；只回产物名与大小，不回绝对路径。"""
    captured: dict = {}
    # 指向仓库里一个**已存在**的文件：既能验证 size 上报，又不需要在沙箱里新建目录。
    existing = REPO_ROOT / "pyproject.toml"
    expected_size = existing.stat().st_size

    def fake_render(params, output_dir):
        captured["params"] = dict(params)
        captured["output_dir"] = str(output_dir)
        return existing

    # ``render_document`` 是**函数内 import**（invoke 里现取），patch 要打在 api 模块上
    monkeypatch.setattr("app.office.api.render_document", fake_render)
    provider = ServerArtifactProvider()
    result = _invoke(
        provider,
        CAPABILITY_ARTIFACT_CREATE,
        {"kind": "document", "title": "季度报告"},
    )
    assert result.ok is True
    assert result.payload["artifacts"][0]["name"] == "pyproject.toml"
    assert result.payload["artifacts"][0]["size"] == expected_size
    assert result.artifact_refs and result.artifact_refs[0].name == "pyproject.toml"
    assert result.served_locally is False
    # 绝对路径绝不进结果（只回名称与大小）。
    assert str(existing.parent) not in str(result.payload)
    assert captured["params"]["title"] == "季度报告"


def test_artifact_provider_failure_becomes_structured_error(monkeypatch):
    def boom(params, output_dir):
        raise RuntimeError("renderer exploded")

    monkeypatch.setattr("app.office.api.render_document", boom)
    result = _invoke(ServerArtifactProvider(), CAPABILITY_ARTIFACT_CREATE, {"kind": "document"})
    assert result.ok is False
    assert result.error_code == "DOCUMENT_RENDER_FAILED"
    assert result.retryable is False


def test_artifact_provider_requires_an_authenticated_user():
    result = _invoke(
        ServerArtifactProvider(),
        CAPABILITY_ARTIFACT_CREATE,
        {"kind": "document"},
        context=_context(user_id=""),
    )
    assert result.ok is False
    assert result.error_code == CapabilityErrorCode.PERMISSION_DENIED.value


def test_register_builtin_providers_is_idempotent_and_overwrites():
    registry = CapabilityRegistry()
    first = register_builtin_providers(registry=registry)
    second = register_builtin_providers(registry=registry)
    assert first == second
    # 每个能力只有一个 Provider 登记（覆盖而不是叠加）。
    for capability in (
        CAPABILITY_ARTIFACT_CREATE,
        CAPABILITY_WORKSPACE_READ,
        CAPABILITY_CODE_EXECUTE,
        CAPABILITY_GIT_OPERATIONS,
    ):
        assert len(registry.registrations(capability)) == 1
    assert len(registry.providers()) == 4
