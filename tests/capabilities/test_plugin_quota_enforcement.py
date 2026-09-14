"""阶段 4：插件 Worker 的**真实**配额执行（灰度 ``PLUGIN_QUOTA_ENFORCEMENT``）。

每个用例都问同一个问题："这是真的执行了，还是只在 Manifest 里声明了？"

============================  ==================================================
用例                          真实证据
============================  ==================================================
超时                          真起子进程 + 真 ``kill()``：子进程"死后才写"的标记文件不存在
输出超限                      真按字节计量；超限时正文落成既有产物，事件只带 ``artifact_id``，无错误码
内置软约束                    只 WARN，不杀进程、不转引用
并发上限                      6 个并发执行，实测峰值 ≤ 声明上限
CPU / 内存 / 网络              POSIX 走 ``RLIMIT_*``（Linux 真生效）；Windows 与网络**只记录**
开关关闭                      不调用 ``enforce_quota()``、不杀、不落产物
============================  ==================================================
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
import sys
import time

import pytest

from lumi_contracts.plugins import PluginManifest
from lumi_contracts.plugins import lifecycle as plugin_lifecycle
from lumi_contracts.plugins.lifecycle import PluginQuota

from app.core.config import settings
from app.plugins.quota import (
    FLAG,
    GenericOutputArtifactSink,
    PluginConcurrencyGate,
    PluginQuotaSpec,
    PluginWorker,
    quota_spec_for,
)

_CASE = itertools.count(1)


def _manifest(**overrides) -> PluginManifest:
    payload = {
        "id": "lumi.batch_runner",
        "version": "3.2.1",
        "kind": "skill_plugin",
        "deployment": "server",
        "data_locality": "cloud",
        "isolation": "sandboxed",
        "trust_level": "official",
        "provides": {"capabilities": ["code.execute@1"]},
    }
    payload.update(overrides)
    return PluginManifest(**payload)


def _spec(*, timeout_seconds: int, max_output_bytes: int, hard: bool = True, **extra) -> PluginQuotaSpec:
    return PluginQuotaSpec(
        plugin_id="lumi.batch_runner",
        plugin_version="3.2.1",
        quota=PluginQuota(
            timeout_seconds=int(timeout_seconds), max_output_bytes=int(max_output_bytes)
        ),
        hard_limits=hard,
        cpu_seconds=float(extra.get("cpu_seconds", 0.0)),
        max_concurrency=int(extra.get("max_concurrency", 1)),
    )


class _RecordingSink:
    """记录正文的产物替身（验证"正文只进产物、事件只有引用"）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def put(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {"artifact_id": "artifact-1", "filename": "out.txt", "size_bytes": len(kwargs["content"])}


@contextlib.contextmanager
def flag(value: bool):
    monkey = pytest.MonkeyPatch()
    monkey.setattr(settings, FLAG, value)
    try:
        yield monkey
    finally:
        monkey.undo()


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


# ── (1) 超时：真杀进程 ────────────────────────────────────


def test_timeout_really_kills_the_worker_and_reports_resource_exceeded(tmp_path):
    marker = tmp_path / "survived.txt"
    code = (
        "import time, pathlib\n"
        "time.sleep(2.5)\n"
        f"pathlib.Path(r'{marker}').write_text('alive', encoding='utf-8')\n"
    )
    with flag(True):
        worker = PluginWorker(spec=_spec(timeout_seconds=1, max_output_bytes=100_000))
        outcome = asyncio.run(worker.run_process(_python(code)))

    assert outcome.action == plugin_lifecycle.QuotaAction.KILL_WORKER.value
    assert outcome.error_code == "PLUGIN_RESOURCE_EXCEEDED"
    assert outcome.killed is True and outcome.blocked is True
    assert "timeout_seconds" in outcome.exceeded
    assert outcome.elapsed_seconds < 2.0, "必须在配额秒数附近就结束，而不是等脚本跑完"
    assert outcome.decision["action"] == "KILL_WORKER"

    # 真杀的证据：脚本"醒来后"才会写的标记文件不存在（等到它本该写完的时间点再看）
    time.sleep(2.6)
    assert not marker.exists(), "进程没有被真正终止"


def test_worker_reports_success_within_quota_and_keeps_the_body():
    with flag(True):
        worker = PluginWorker(spec=_spec(timeout_seconds=20, max_output_bytes=100_000))
        outcome = asyncio.run(worker.run_process(_python("print('hello-plugin')")))
    assert outcome.ok is True
    assert outcome.action == plugin_lifecycle.QuotaAction.NONE.value
    assert outcome.error_code == ""
    assert "hello-plugin" in outcome.content
    assert outcome.result_ref is None


# ── (2) 输出超限：转产物引用（不报错） ────────────────────


def test_output_over_limit_becomes_an_artifact_ref_without_an_error_code():
    sink = _RecordingSink()
    with flag(True):
        worker = PluginWorker(spec=_spec(timeout_seconds=20, max_output_bytes=1024), artifact_sink=sink)
        outcome = asyncio.run(worker.run_process(_python("print('x' * 5000)")))

    assert outcome.action == plugin_lifecycle.QuotaAction.ARTIFACT_REF.value
    assert outcome.error_code == "", "输出超限不是错误"
    assert outcome.blocked is False and outcome.ok is True
    assert outcome.result_ref and outcome.result_ref["artifact_id"] == "artifact-1"
    assert outcome.content == "", "正文进产物，事件里不得再带正文"
    assert outcome.output_bytes >= 5000, "按**真实**字节计量（不是截断后的体积）"
    assert outcome.exceeded == ("max_output_bytes",)
    assert "max_output_bytes" in outcome.decision["exceeded"]
    assert len(sink.calls[0]["content"]) >= 1024


def test_artifact_ref_is_resolvable_through_the_existing_artifact_api(monkeypatch, tmp_path):
    """默认产物落点复用既有 `office_outputs` + 既有 `artifact_id`（可用既有 API 取回）。"""
    from app.services.artifacts import artifact_path, artifact_record

    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "uploads"))
    with flag(True):
        worker = PluginWorker(
            spec=_spec(timeout_seconds=20, max_output_bytes=512),
            artifact_sink=GenericOutputArtifactSink(),
            owner="u-stage4",
            container="conv-stage4",
        )
        outcome = asyncio.run(worker.run_process(_python("print('y' * 4096)")))

    artifact_id = str(outcome.result_ref["artifact_id"])
    path = artifact_path("u-stage4", artifact_id)
    assert path is not None and path.is_file(), "产物必须能被既有 artifact API 解析"
    assert path.read_bytes().startswith(b"y")
    assert artifact_record("u-stage4", artifact_id)["size_bytes"] == path.stat().st_size
    assert artifact_path("someone-else", artifact_id) is None, "归属校验仍然生效"


# ── (3) 内置插件：软约束只 WARN ───────────────────────────


def test_builtin_soft_limit_only_warns_and_never_kills():
    spec = _spec(timeout_seconds=1, max_output_bytes=100_000, hard=False)
    with flag(True):
        worker = PluginWorker(spec=spec)
        outcome = asyncio.run(
            worker.run_process(_python("import time; time.sleep(1.5); print('done')"))
        )
    assert outcome.action == plugin_lifecycle.QuotaAction.WARN.value
    assert outcome.error_code == "PLUGIN_RESOURCE_EXCEEDED"
    assert outcome.blocked is False and outcome.killed is False, "内置插件软约束不杀进程"
    assert "done" in outcome.content, "软约束不丢正文、不转引用"
    assert outcome.result_ref is None
    assert "timeout_seconds" in outcome.exceeded


def test_builtin_trust_level_maps_to_soft_limits():
    builtin = quota_spec_for(_manifest(trust_level="builtin", isolation="in_process"))
    third_party = quota_spec_for(_manifest(trust_level="third_party", isolation="sandboxed"))
    assert builtin.hard_limits is False
    assert third_party.hard_limits is True


# ── (4) 并发上限：真限流 ──────────────────────────────────


def test_concurrency_cap_really_serialises_worker_runs():
    gate = PluginConcurrencyGate()
    with flag(True):
        worker = PluginWorker(
            spec=_spec(timeout_seconds=20, max_output_bytes=100_000, max_concurrency=2), gate=gate
        )

        async def scenario():
            return await asyncio.gather(
                *[
                    worker.run_process(_python("import time; time.sleep(0.4)"))
                    for _ in range(6)
                ]
            )

        outcomes = asyncio.run(scenario())

    assert len(outcomes) == 6 and all(item.ok for item in outcomes)
    snapshot = gate.snapshot("lumi.batch_runner")
    assert snapshot["limit"] == 2
    assert snapshot["peak"] <= 2, f"并发峰值必须被真限制住：{snapshot}"


# ── (5) 沙箱接线：真进程路径复用同一套配额 ────────────────


def test_local_sandbox_enforces_the_plugin_quota():
    from app.agents.sandbox.local import LocalSandbox

    with flag(True):
        result = asyncio.run(
            LocalSandbox().run_script(
                "import time; time.sleep(30)",
                timeout=30,
                quota_spec=_spec(timeout_seconds=1, max_output_bytes=100_000),
            )
        )
    assert result.status == "timeout"
    assert result.resource_usage["error_code"] == "PLUGIN_RESOURCE_EXCEEDED"
    assert result.resource_usage["killed"] is True
    assert result.resource_usage["plugin_version"] == "3.2.1"
    assert result.resource_usage["quota"]["action"] == "KILL_WORKER"


def test_local_sandbox_externalises_oversized_output(monkeypatch, tmp_path):
    from app.agents.sandbox.local import LocalSandbox

    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "uploads"))
    with flag(True):
        result = asyncio.run(
            LocalSandbox().run_script(
                "print('z' * 5000)",
                timeout=30,
                quota_spec=_spec(timeout_seconds=20, max_output_bytes=1024),
            )
        )
    assert result.status == "success", "输出超限不是执行失败"
    usage = result.resource_usage
    assert usage["quota_action"] == "ARTIFACT_REF"
    assert usage["result_ref"]["artifact_id"]
    assert usage["output_bytes"] >= 5000
    assert "error_code" not in usage


def test_manifest_declaration_is_what_the_worker_enforces():
    """声明 → 执行：Manifest 的 resource_limits 就是 Worker 用的配额（不是默认值）。"""
    manifest = _manifest(
        resource_limits={
            "memory_mb": 128,
            "cpu_seconds": 7.5,
            "wall_seconds": 9.0,
            "max_output_bytes": 4096,
            "max_concurrency": 3,
        }
    )
    spec = quota_spec_for(manifest)
    assert spec.plugin_id == "lumi.batch_runner" and spec.plugin_version == "3.2.1"
    assert spec.quota.memory_mb == 128
    assert spec.quota.timeout_seconds == 9
    assert spec.quota.max_output_bytes == 4096
    assert spec.cpu_seconds == 7.5 and spec.max_concurrency == 3
    assert spec.hard_limits is True


# ── (6) 只记录 vs 真执行 ──────────────────────────────────


def test_limits_that_are_only_recorded_are_declared_as_such():
    """诚实清单：核数 / 网络 / （Windows 下的）CPU·内存**只记录**，不假装拦截。"""
    spec = quota_spec_for(
        _manifest(
            resource_limits={"memory_mb": 64, "cpu_seconds": 3.0, "wall_seconds": 5.0,
                             "max_output_bytes": 2048, "max_concurrency": 2}
        )
    )
    payload = spec.as_dict()
    assert payload["memory_mb"] == 64 and payload["max_output_bytes"] == 2048
    assert payload["cpu_seconds"] == 3.0 and payload["max_concurrency"] == 2
    # network_* 只有声明值，没有任何执行证据 —— 保持原样、不谎报
    assert spec.quota.network_egress == "allowlist"
    assert spec.quota.network_rate_limit == "10/min"
    # 核数配额需要 cgroup/容器；本仓库没有 → 只记录
    assert "cpu_limit" in payload
    # 非 POSIX 平台没有 resource 模块 → RLIMIT 不可用（真执行只在 Linux 容器里发生）
    from app.plugins.quota import _preexec_limits

    if os.name != "posix":
        assert _preexec_limits(3.0, 64) is None
    else:  # pragma: no cover - Linux/容器
        assert callable(_preexec_limits(3.0, 64))


# ── (7) 开关关闭：不咨询配额契约 ──────────────────────────


def test_flag_off_never_consults_the_quota_contract(monkeypatch):
    def _boom(*args, **kwargs):  # pragma: no cover - 只在被误调用时触发
        raise AssertionError("开关关闭时不得咨询配额契约")

    monkeypatch.setattr(plugin_lifecycle, "enforce_quota", _boom)
    sink = _RecordingSink()
    with flag(False):
        worker = PluginWorker(spec=_spec(timeout_seconds=1, max_output_bytes=8), artifact_sink=sink)
        outcome = asyncio.run(worker.run_process(_python("print('x' * 5000)"), timeout=30))

    assert outcome.action == plugin_lifecycle.QuotaAction.NONE.value
    assert outcome.decision == {}
    assert outcome.result_ref is None and sink.calls == []
    assert outcome.output_bytes >= 5000, "开关关闭时不按配额计量，正文照旧返回"
    assert "x" * 100 in outcome.content


def test_flag_off_local_sandbox_keeps_the_legacy_path(monkeypatch):
    from app.agents.sandbox.local import LocalSandbox

    with flag(False):
        result = asyncio.run(
            LocalSandbox().run_script(
                "print('legacy-ok')",
                timeout=30,
                quota_spec=_spec(timeout_seconds=1, max_output_bytes=8),
            )
        )
    assert result.status == "success"
    assert "legacy-ok" in result.stdout
    assert "quota" not in result.resource_usage
    assert "quota_action" not in result.resource_usage


def test_flag_default_is_off():
    assert settings.PLUGIN_QUOTA_ENFORCEMENT is False
    assert FLAG == "PLUGIN_QUOTA_ENFORCEMENT"


# ── (8) 执行边界：协作式 vs 强制终止（阶段 4 收口） ────────


@pytest.fixture(autouse=True)
def _clean_quota_evidence():
    """执行证据是进程内计数：每个用例前后都清空，避免相互污染。"""
    from app.plugins.boundary import reset_quota_evidence

    reset_quota_evidence()
    yield
    reset_quota_evidence()


def _reason_spec(
    *, cpu_seconds: float = 0.0, memory_mb: int = 0, wall_seconds: int = 0, hard: bool = True
) -> PluginQuotaSpec:
    return PluginQuotaSpec(
        plugin_id="lumi.batch_runner",
        plugin_version="3.2.1",
        quota=PluginQuota(
            timeout_seconds=int(wall_seconds),
            memory_mb=int(memory_mb),
            max_output_bytes=1000,
        ),
        cpu_seconds=float(cpu_seconds),
        hard_limits=hard,
    )


def test_hard_termination_reasons_follow_the_declared_limits():
    from lumi_contracts.plugins.execution import TerminationReason

    everything = _reason_spec(cpu_seconds=2.0, memory_mb=64, wall_seconds=5)
    assert set(everything.hard_termination_reasons) == {
        TerminationReason.CPU_LIMIT.value,
        TerminationReason.MEMORY_LIMIT.value,
        TerminationReason.RUNAWAY_LOOP.value,
    }
    # 逐项关掉声明值 → 对应原因消失（不虚报"需要强杀"）
    only_cpu = _reason_spec(cpu_seconds=2.0)
    assert only_cpu.hard_termination_reasons == (TerminationReason.CPU_LIMIT.value,)
    # 软约束（内置插件）永远不需要强杀
    assert _reason_spec(cpu_seconds=2.0, memory_mb=64, wall_seconds=5, hard=False).hard_termination_reasons == ()


def test_in_process_worker_run_refuses_forced_reasons_only():
    from lumi_contracts.plugins.execution import (
        PLUGIN_QUOTA_NOT_ENFORCED,
        TerminationReason,
    )

    async def body() -> str:
        return "still-here"

    with flag(True):
        worker = PluginWorker(spec=_spec(timeout_seconds=5, max_output_bytes=1000))
        refused = asyncio.run(worker.run(body, reason=TerminationReason.RUNAWAY_CHILD_PROCESS.value))
        allowed = asyncio.run(worker.run(body, reason=TerminationReason.TIMEOUT_NOTIFY.value))

    assert refused.ok is False and refused.blocked is True, "需要强杀时不许悄悄按协作式跑"
    assert refused.action == plugin_lifecycle.QuotaAction.REFUSED.value
    assert refused.error_code == PLUGIN_QUOTA_NOT_ENFORCED
    assert refused.content == ""
    assert allowed.ok is True and allowed.content == "still-here"
    assert allowed.action == plugin_lifecycle.QuotaAction.NONE.value


def test_real_process_runs_are_recorded_as_forced_evidence():
    """只有真走可强杀进程的执行才算 forced；协作式永远不算 enforced。"""
    from app.plugins.boundary import evidence_for

    with flag(True):
        worker = PluginWorker(spec=_spec(timeout_seconds=20, max_output_bytes=100_000))
        outcome = asyncio.run(worker.run_process(_python("print('forced-ok')")))
    assert outcome.ok is True
    evidence = evidence_for("lumi.batch_runner").as_dict()
    assert evidence["forced"] == 1 and evidence["cooperative"] == 0
    assert evidence["observed"] == 1 and evidence["wired"] == 0
    assert evidence["cooperative_only"] is False, "wired=0 时不得声称协作式-only"
