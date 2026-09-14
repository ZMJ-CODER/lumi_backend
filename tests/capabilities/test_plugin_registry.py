"""阶段 4 回归：服务端 Skill Plugin 生命周期（安装/门禁/依赖/版本/回滚/快照）。

插件系统最容易"看起来能用"的地方是门禁：类型没登记也装上、签名自称官方就当官方、
依赖没装也激活、停用把别人的依赖打挂。这里逐条钉住。

同时覆盖阶段 8 的后端部分：未知 kind 默认拒绝，登记为 Extension Handler 也只在
开发者模式放行。
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import itertools
import json
import os
from pathlib import Path

import pytest

from lumi_contracts.plugins import IsolationLevel, PluginManifest, TrustLevel

from app.plugins import (
    PLUGIN_DEPENDENCIES_UNSATISFIED,
    PLUGIN_KIND_UNKNOWN,
    PLUGIN_SIGNATURE_INVALID,
    PLUGIN_VERSION_UNAVAILABLE,
    DependencyResolver,
    PluginRegistry,
    PluginRejected,
    PluginStateStore,
    SignaturePolicy,
)
from app.plugins.extensions import ExtensionHandlerRegistry
from app.plugins.signatures import signed_payload


from _paths import REPO_ROOT
_STATE_COUNTER = itertools.count(1)


@contextlib.contextmanager
def state_dir():
    """状态文件放在 ``.ptmp``（工作区内的临时目录，已被 git 忽略）。

    产品代码本身是"建目录 + 原子写、失败降级为仅内存"；这里给一个已存在且可写的位置，
    才能验证"安装状态真的落盘、重启后仍记得"这条路径。文件名带 PID 与序号，互不干扰。
    """
    base = REPO_ROOT / ".ptmp"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"plugin_state_{os.getpid()}_{next(_STATE_COUNTER)}.json"
    try:
        yield path
    finally:
        for target in (path, path.with_name(f"{path.name}.tmp")):
            with contextlib.suppress(OSError):
                target.unlink()


def _manifest(**overrides) -> PluginManifest:
    payload = {
        "id": "lumi.code_expert",
        "version": "1.0.0",
        "kind": "skill_plugin",
        "deployment": "server",
        "data_locality": "cloud",
        # 未配置验签密钥时会被降级为 third_party，因此默认隔离取 sandboxed；
        # 已验签的 official 插件用 restricted_worker（见签名用例）。
        "isolation": "sandboxed",
        "trust_level": "official",
        "provides": {"capabilities": ["code.execute@1"]},
    }
    payload.update(overrides)
    return PluginManifest(**payload)


def _registry(path: Path, **kwargs) -> PluginRegistry:
    return PluginRegistry(store=PluginStateStore(state_dir=path), **kwargs)


# ── 安装与门禁 ───────────────────────────────────────────────────


def test_install_registers_plugin_and_persists_state():
    with state_dir() as path:
        registry = _registry(path)
        installation = registry.install(_manifest())
        assert installation.plugin_id == "lumi.code_expert"
        assert installation.enabled is True
        # 未配置验签密钥 → 未验签，且自称 official 被降级为第三方。
        assert installation.verified is False
        assert installation.manifest.trust_level is TrustLevel.THIRD_PARTY
        assert registry.get("lumi.code_expert") is not None
        # 状态落盘：重启后仍然记得
        reopened = _registry(path)
        assert reopened.get("lumi.code_expert") is not None
        assert reopened.get("lumi.code_expert").version == "1.0.0"


def test_unknown_plugin_kind_is_rejected_with_stable_code():
    """未知插件类型：契约层拒绝，并由 ``parse_manifest`` 折成稳定错误码。"""
    with pytest.raises(PluginRejected) as info:
        PluginRegistry.parse_manifest(
            {"id": "lumi.quantum", "version": "1.0.0", "kind": "quantum_plugin"}
        )
    assert info.value.code == PLUGIN_KIND_UNKNOWN
    # Manifest 本身不合法（id/version）走另一个稳定码，便于前端区分处置。
    with pytest.raises(PluginRejected) as info:
        PluginRegistry.parse_manifest({"id": "Bad Id", "version": "v1", "kind": "skill_plugin"})
    assert info.value.code == "PLUGIN_MANIFEST_INVALID"


def test_extension_registry_is_the_lookup_point_for_unknown_kinds():
    """Extension Handler 是"未知 kind 能不能用"的唯一查询点（生产默认不在册）。"""
    local = ExtensionHandlerRegistry()
    assert local.handles("ext.quantum") is False
    local.register(
        kind="ext.quantum",
        handler_id="lumi.ext.quantum",
        isolation=IsolationLevel.SANDBOXED,
        reason="内部实验类型",
        audited=True,
    )
    assert local.handles("ext.quantum") is True
    assert local.get("ext.quantum").audited is True
    assert local.to_snapshot()[0]["kind"] == "ext.quantum"
    assert local.unregister("ext.quantum") is True
    assert local.handles("ext.quantum") is False


def test_extension_registry_refuses_builtin_kinds_and_weak_isolation():
    local = ExtensionHandlerRegistry()
    with pytest.raises(ValueError, match="必须以 ext."):
        local.register(kind="skill_plugin", handler_id="x")
    # ``ext.skill_plugin`` 归一化后是内置类型 skill_plugin → 拒绝。
    with pytest.raises(ValueError, match="必须以 ext.|内置插件类型"):
        local.register(kind="ext.skill_plugin", handler_id="x")
    # 扩展类型不允许进程内运行（没经过内置审计）。
    with pytest.raises(ValueError, match="隔离"):
        local.register(kind="ext.custom", handler_id="x", isolation=IsolationLevel.IN_PROCESS)
    assert local.handles("ext.custom") is False


def test_weak_isolation_is_rejected_for_untrusted_plugins():
    """第三方插件不允许进程内运行；契约层就先拒绝（比安装期更早失败也算）。"""
    with pytest.raises(Exception) as info:
        _manifest(id="lumi.sneaky", trust_level="third_party", isolation="in_process")
    assert "隔离强度不足" in str(info.value)


    # 已验签的 official 插件才允许 restricted_worker；这里验证降级后不再满足。
    with state_dir() as path:
        policy = SignaturePolicy(algorithm="hmac-sha256", secret="s3cret")
        registry = _registry(path)
        with pytest.raises(PluginRejected) as info:
            registry.install(
                _manifest(
                    id="lumi.declares_worker",
                    isolation="restricted_worker",
                    signature={"algorithm": "hmac-sha256", "value": "bad"},
                ),
                signature_policy=policy,
            )
        assert info.value.code == PLUGIN_SIGNATURE_INVALID


def test_unverified_official_is_downgraded_not_trusted():
    """没有验签设施时不能自称 official；降级后必须满足第三方隔离要求。"""
    with state_dir() as path:
        registry = _registry(path)
        installation = registry.install(
            _manifest(id="lumi.claims_official", isolation="sandboxed")
        )
        assert installation.verified is False
        assert installation.manifest.trust_level is TrustLevel.THIRD_PARTY
        # API 视图把"为什么没被信任"一起返回，安装界面才能解释。
        assert installation.to_api()["signature_reason"]


# ── 签名 ─────────────────────────────────────────────────────────


def test_configured_signature_is_verified_and_preserves_official_trust():
    with state_dir() as path:
        policy = SignaturePolicy(algorithm="hmac-sha256", secret="s3cret", key_id="k1")
        manifest = _manifest(
            signature={"algorithm": "hmac-sha256", "key_id": "k1", "value": "pending"}
        )
        expected = hmac.new(
            b"s3cret", signed_payload(manifest, None), hashlib.sha256
        ).hexdigest()
        signed = manifest.model_copy(
            update={"signature": manifest.signature.model_copy(update={"value": expected})}
        )
        registry = _registry(path)
        installation = registry.install(signed, signature_policy=policy)
        assert installation.verified is True
        assert installation.manifest.trust_level is TrustLevel.OFFICIAL


def test_bad_signature_is_rejected_when_key_is_configured():
    with state_dir() as path:
        policy = SignaturePolicy(algorithm="hmac-sha256", secret="s3cret")
        manifest = _manifest(
            signature={"algorithm": "hmac-sha256", "value": "deadbeef"}
        )
        registry = _registry(path)
        with pytest.raises(PluginRejected) as info:
            registry.install(manifest, signature_policy=policy)
        assert info.value.code == PLUGIN_SIGNATURE_INVALID


# ── 依赖 ─────────────────────────────────────────────────────────


def test_missing_plugin_dependency_blocks_install():
    with state_dir() as path:
        registry = _registry(path)
        with pytest.raises(PluginRejected) as info:
            registry.install(
                _manifest(id="lumi.dependent", requires={"plugins": ["lumi.missing>=1.0.0"]})
            )
        assert info.value.code == PLUGIN_DEPENDENCIES_UNSATISFIED
        assert info.value.details["issues"][0]["code"] == "PLUGIN_MISSING"
        # 依赖不满足时**不落库**（不留半装状态）。
        assert registry.get("lumi.dependent") is None


def test_capability_dependency_without_provider_is_degraded_not_blocked():
    """客户端 Provider 未连接时能力暂时不可用，但安装不该失败。"""
    resolver = DependencyResolver(registry=None)
    report = resolver.resolve(
        _manifest(id="lumi.reader", requires={"capabilities": ["workspace.read@1"]})
    )
    assert report.ok is True
    assert report.state == "degraded"
    assert report.capability_availability["workspace.read@1"] is False


def test_unknown_capability_dependency_is_blocking():
    resolver = DependencyResolver(registry=None)
    report = resolver.resolve(
        _manifest(id="lumi.reader", requires={"capabilities": ["nope.nothing@1"]})
    )
    assert report.ok is False
    assert report.state == "unavailable"
    assert report.blocking[0].code == "CAPABILITY_UNKNOWN"


def test_version_constraint_is_enforced_for_plugin_dependencies():
    resolver = DependencyResolver(registry=None)
    report = resolver.resolve(
        _manifest(requires={"plugins": ["other.plugin>=2.0.0"]}),
        installed=[{"plugin_id": "other.plugin", "version": "1.4.0", "enabled": True}],
    )
    assert report.ok is False
    assert report.blocking[0].code == "PLUGIN_VERSION_MISMATCH"


# ── 启用 / 停用 / 升级 / 回滚 ────────────────────────────────────


def test_disable_is_blocked_while_enabled_dependents_exist():
    with state_dir() as path:
        registry = _registry(path)
        registry.install(_manifest())
        registry.install(
            _manifest(
                id="lumi.dependent",
                provides={"capabilities": ["artifact.create@1"]},
                requires={"plugins": ["lumi.code_expert>=1.0.0"]},
            )
        )
        with pytest.raises(PluginRejected) as info:
            registry.disable("lumi.code_expert")
        assert info.value.code == PLUGIN_DEPENDENCIES_UNSATISFIED
        # 先停用依赖方，再停用被依赖方
        registry.disable("lumi.dependent")
        disabled = registry.disable("lumi.code_expert")
        assert disabled.enabled is False


def test_upgrade_keeps_rollback_point_and_rollback_disables_until_reenabled():
    with state_dir() as path:
        registry = _registry(path)
        registry.install(_manifest())
        upgraded = registry.upgrade(_manifest(version="1.1.0"))
        assert upgraded.version == "1.1.0"
        assert upgraded.previous_version == "1.0.0"
        with pytest.raises(PluginRejected) as info:
            registry.upgrade(_manifest(version="1.1.0"))
        assert info.value.code == PLUGIN_VERSION_UNAVAILABLE

        rolled = registry.rollback("lumi.code_expert")
        assert rolled.version == "1.0.0"
        assert rolled.previous_version == "1.1.0"
        # 回滚后必须重新启用（重新过门禁），不悄悄恢复运行。
        assert rolled.enabled is False
        reenabled = registry.enable("lumi.code_expert")
        assert reenabled.enabled is True


def test_health_check_reports_unhealthy_for_unresolvable_module():
    """内置（已验签）插件的 import 健康检查真的会探测；探测不到即 unhealthy。"""
    with state_dir() as path:
        policy = SignaturePolicy(algorithm="hmac-sha256", secret="s3cret")
        manifest = _manifest(
            id="lumi.broken",
            trust_level="builtin",
            isolation="in_process",
            entrypoints={"module": "definitely_not_a_module_xyz"},
            healthcheck={"kind": "import", "target": "definitely_not_a_module_xyz"},
        )
        signed = manifest.model_copy(
            update={
                "signature": manifest.signature.model_copy(
                    update={
                        "algorithm": "hmac-sha256",
                        "value": hmac.new(
                            b"s3cret", signed_payload(manifest, None), hashlib.sha256
                        ).hexdigest(),
                    }
                )
            }
        )
        registry = _registry(path)
        installation = registry.install(signed, signature_policy=policy, activate=False)
        assert installation.verified is True
        assert installation.manifest.trust_level is TrustLevel.BUILTIN
        assert installation.health_status == "unhealthy"
        with pytest.raises(PluginRejected) as info:
            registry.enable("lumi.broken")
        assert info.value.code == "PLUGIN_STATE_UNAVAILABLE"


def test_third_party_health_is_probed_by_worker_not_in_api_process():
    """第三方插件不在 API 进程内 import 探测（隔离边界），健康状态如实报 unknown。"""
    with state_dir() as path:
        registry = _registry(path)
        installation = registry.install(
            _manifest(
                id="lumi.third_party",
                isolation="sandboxed",
                entrypoints={"module": "definitely_not_a_module_xyz"},
                healthcheck={"kind": "import", "target": "definitely_not_a_module_xyz"},
            ),
            activate=False,
        )
        assert installation.manifest.trust_level is TrustLevel.THIRD_PARTY
        assert installation.health_status == "unknown"
        assert "Worker" in installation.health_detail


def test_uninstall_requires_no_enabled_dependents():
    with state_dir() as path:
        registry = _registry(path)
        registry.install(_manifest())
        registry.install(
            _manifest(
                id="lumi.dependent",
                provides={"capabilities": ["artifact.create@1"]},
                requires={"plugins": ["lumi.code_expert"]},
            )
        )
        with pytest.raises(PluginRejected):
            registry.uninstall("lumi.code_expert")
        registry.disable("lumi.dependent")
        assert registry.uninstall("lumi.code_expert") is True
        assert registry.get("lumi.code_expert") is None


# ── 快照 ─────────────────────────────────────────────────────────


def test_plugin_snapshot_separates_skills_providers_and_policies():
    with state_dir() as path:
        registry = _registry(path)
        registry.install(_manifest())
        registry.install(
            _manifest(
                id="lumi.local.provider",
                kind="capability_provider",
                deployment="client",
                data_locality="local_only",
                isolation="client_device",
                trust_level="official",
                provides={"capabilities": ["workspace.read@1"]},
                entrypoints={"capability": "workspace.read"},
            )
        )
        registry.install(
            _manifest(
                id="lumi.policy.manual",
                kind="policy_pack",
                isolation="sandboxed",
                trust_level="third_party",
                provides={"policies": ["manual_commit"]},
                entrypoints={"policy_id": "manual_commit"},
            )
        )
        snapshot = registry.plugin_snapshot()
        assert [item["id"] for item in snapshot["skills"]] == ["lumi.code_expert"]
        assert [item["id"] for item in snapshot["providers"]] == ["lumi.local.provider"]
        assert snapshot["policies"][0]["id"] == "lumi.policy.manual"
        # 快照可 JSON 化（要进 routing）
        assert json.loads(json.dumps(snapshot, ensure_ascii=False)) == snapshot
        api = registry.get("lumi.code_expert").to_api()
        assert api["data_leaves_device"] is True
        assert api["enabled"] is True


def test_install_activate_false_keeps_plugin_disabled():
    with state_dir() as path:
        registry = _registry(path)
        installation = registry.install(_manifest(), activate=False)
        assert installation.enabled is False
        assert registry.enabled() == []
        assert registry.enable("lumi.code_expert").enabled is True
