"""阶段 4：插件 Worker 的**真实**资源配额边界（方案 §4.3）。

Manifest 里的 ``resource_limits`` 只是**声明**；本模块把它变成 Worker 侧真执行的约束：

============================  ==========================================================
约束                          真实行为
============================  ==========================================================
``timeout_seconds``           ``Popen.wait(timeout)`` 超时 → ``kill()`` **真杀进程** + ``PLUGIN_RESOURCE_EXCEEDED``
``max_output_bytes``          真按字节计量；超限 → 正文落成**既有产物**（``artifact_id``）并只回引用，**不报错**
``max_concurrency``           每插件信号量：超过就排队，绝不并发跑（``PluginConcurrencyGate``）
``cpu_seconds``               POSIX ``RLIMIT_CPU``（Linux 生产真生效）；Windows 无 ``resource`` → 只记录
``memory_mb``                 POSIX ``RLIMIT_AS``（同上）；Windows 只记录
``network_*``                 无网络命名空间可用 → **只记录**，不假装拦截
``cpu_limit``（核数）         只记录（内核级配额需要容器/cgroup；容器路径见 ``DockerSandbox``）
============================  ==========================================================

两条硬规则来自契约（:mod:`lumi_contracts.plugins.lifecycle`），本模块不重新解释：

* 输出超限 ``ARTIFACT_REF``：正文进产物、事件只带 ``result_ref``，**不是失败**；
* 时间超限 ``KILL_WORKER``：杀 Worker + ``PLUGIN_RESOURCE_EXCEEDED``；
* 内置插件 ``hard_limits=False``：只 ``WARN``，不杀。

灰度 ``PLUGIN_QUOTA_ENFORCEMENT`` 关闭时本模块**不做任何判定**：不调用
``enforce_quota()``、不杀进程、不落产物（见 ``tests/test_plugin_quota_enforcement.py``）。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

from loguru import logger

from lumi_contracts.plugins import lifecycle as plugin_lifecycle
from lumi_contracts.plugins.execution import (
    PLUGIN_QUOTA_NOT_ENFORCED,
    TerminationMode,
    TerminationReason,
    requires_forced_termination,
)
from lumi_contracts.plugins.lifecycle import PluginQuota

from app.core.feature_flags import feature_enabled
from app.services.plugins.manager import FLAG

#: 开关关闭时的兜底墙钟（与既有沙箱默认一致）。
DEFAULT_WALL_SECONDS = 30.0
#: 读取输出时的分块大小。
_READ_CHUNK = 65536


@dataclass(frozen=True, slots=True)
class PluginQuotaSpec:
    """一个已安装插件的完整配额视图（契约 ``PluginQuota`` + 真实执行所需的额外项）。"""

    plugin_id: str
    quota: PluginQuota
    plugin_version: str = ""
    cpu_seconds: float = 0.0
    max_concurrency: int = 1
    #: ``False`` = 内置插件（软约束，只 WARN）。
    hard_limits: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            **self.quota.model_dump(mode="json"),
            "cpu_seconds": float(self.cpu_seconds),
            "max_concurrency": int(self.max_concurrency),
            "hard_limits": bool(self.hard_limits),
        }

    @property
    def hard_termination_reasons(self) -> tuple[str, ...]:
        """该配额**必须**具备强杀能力的原因（空 = 协作式就够）。

        硬约束下：CPU 秒数、内存上限、墙钟超时都只能靠"真杀进程"兜住，
        进程内 ``cancel()`` 拦不住死循环与失控子进程。
        """
        if not self.hard_limits:
            return ()
        reasons: list[str] = []
        if float(self.cpu_seconds or 0.0) > 0:
            reasons.append(TerminationReason.CPU_LIMIT.value)
        if int(self.quota.memory_mb or 0) > 0:
            reasons.append(TerminationReason.MEMORY_LIMIT.value)
        if float(self.quota.timeout_seconds or 0.0) > 0:
            reasons.append(TerminationReason.RUNAWAY_LOOP.value)
        return tuple(reasons)


def quota_spec_for(source: Any, *, plugin_version: str = "") -> PluginQuotaSpec:
    """``PluginInstallation`` / ``PluginManifest`` → 配额视图（Manifest 声明是唯一来源）。

    ``trust_level == builtin`` 的插件按**软约束**处理（契约：内置插件不杀进程）。
    """
    manifest = getattr(source, "manifest", source)
    limits = getattr(manifest, "resource_limits", None)
    trust = str(getattr(getattr(manifest, "trust_level", ""), "value", "") or "")
    version = str(plugin_version or getattr(source, "version", "") or getattr(manifest, "version", "") or "")
    quota = PluginQuota(
        memory_mb=int(getattr(limits, "memory_mb", PluginQuota().memory_mb)),
        timeout_seconds=max(1, int(round(float(getattr(limits, "wall_seconds", 60.0) or 60.0)))),
        max_output_bytes=max(1024, int(getattr(limits, "max_output_bytes", 2_000_000))),
    )
    return PluginQuotaSpec(
        plugin_id=str(getattr(manifest, "id", "") or ""),
        quota=quota,
        plugin_version=version,
        cpu_seconds=max(0.0, float(getattr(limits, "cpu_seconds", 0.0) or 0.0)),
        max_concurrency=max(1, int(getattr(limits, "max_concurrency", 1) or 1)),
        hard_limits=trust != "builtin",
    )


# ── 输出超限 → 既有产物引用（不报错） ─────────────────────


class PluginArtifactSink(Protocol):
    """超限正文的落盘边界（返回**引用**，调用方不得拿到正文）。"""

    async def put(
        self,
        *,
        plugin_id: str,
        plugin_version: str,
        content: str,
        output_bytes: int,
        media_type: str,
        owner: str,
        container: str,
    ) -> dict[str, Any]: ...


class GenericOutputArtifactSink:
    """复用**既有通用产物**目录（``office_outputs/<user>/<container>/``）+ 既有 ``artifact_id``。

    产物可用既有 ``app.services.artifacts.artifact_path(user_id, artifact_id)`` 解析，
    因此不需要新建产物存储，也不需要新的事件字段。
    """

    def __init__(self, *, suffix: str = ".txt") -> None:
        self._suffix = str(suffix or ".txt")

    async def put(
        self,
        *,
        plugin_id: str,
        plugin_version: str,
        content: str,
        output_bytes: int,
        media_type: str,
        owner: str = "",
        container: str = "",
    ) -> dict[str, Any]:
        from app.services import office_docs
        from app.services.artifacts import artifact_from_output

        safe_id = "".join(ch for ch in str(plugin_id) if ch.isalnum() or ch in "._-")[:80] or "plugin"
        container_id = str(container or f"plugin-{safe_id}")
        name = f"{safe_id}-output-{int(time.time())}{self._suffix}"
        directory = office_docs.generic_outputs_dir(str(owner or "plugin"), container_id)
        payload = str(content).encode("utf-8")

        def _write() -> None:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / name).write_bytes(payload)

        await asyncio.to_thread(_write)
        ref = artifact_from_output(container_id, {"name": name, "size": len(payload)})
        return {
            **dict(ref or {}),
            "plugin_id": str(plugin_id),
            "plugin_version": str(plugin_version),
            "output_bytes": int(output_bytes),
            "media_type": str(media_type),
        }


# ── 并发上限（真执行） ────────────────────────────────────


class PluginConcurrencyGate:
    """每插件并发上限：超过就排队（同插件绝不并发超限）。"""

    def __init__(self) -> None:
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._limits: dict[str, int] = {}
        self._active: dict[str, int] = {}
        self._peak: dict[str, int] = {}

    def _semaphore(self, plugin_id: str, limit: int) -> asyncio.Semaphore:
        limit = max(1, int(limit))
        current = self._semaphores.get(plugin_id)
        if current is None:
            current = asyncio.Semaphore(limit)
            self._semaphores[plugin_id] = current
            self._limits[plugin_id] = limit
            return current
        # 上限变化只在没有在跑时替换信号量（避免旧持有者释放到新信号量上）。
        if self._limits.get(plugin_id) != limit and not self._active.get(plugin_id):
            current = asyncio.Semaphore(limit)
            self._semaphores[plugin_id] = current
            self._limits[plugin_id] = limit
        return current

    @asynccontextmanager
    async def hold(self, plugin_id: str, limit: int):
        key = str(plugin_id)
        semaphore = self._semaphore(key, limit)
        await semaphore.acquire()
        self._active[key] = int(self._active.get(key, 0)) + 1
        self._peak[key] = max(int(self._peak.get(key, 0)), self._active[key])
        try:
            yield
        finally:
            self._active[key] = max(0, int(self._active.get(key, 1)) - 1)
            semaphore.release()

    def snapshot(self, plugin_id: str) -> dict[str, Any]:
        key = str(plugin_id)
        return {
            "plugin_id": key,
            "limit": int(self._limits.get(key, 0)),
            "active": int(self._active.get(key, 0)),
            "peak": int(self._peak.get(key, 0)),
        }

    def reset(self) -> None:
        """仅测试用：清空信号量（跨事件循环复用同一个 gate 时必须）。"""
        self._semaphores.clear()
        self._limits.clear()
        self._active.clear()
        self._peak.clear()


#: 进程内共享并发门（与 ``capability_registry`` 同风格的单例）。
plugin_concurrency_gate = PluginConcurrencyGate()


# ── 执行证据（只由真实执行边界写；见 app.services.plugins.boundary） ──


def _record_evidence(
    spec: PluginQuotaSpec,
    *,
    mode: str,
    output_bytes: int,
    action: str,
    killed: bool = False,
    error_code: str = "",
    elapsed_seconds: float = 0.0,
) -> None:
    """记录"这次真的执行了"（``observed``；``forced`` 只在可强杀进程里记）。"""
    try:
        from app.services.plugins.boundary import record_execution

        record_execution(
            spec.plugin_id,
            mode=mode,
            output_bytes=output_bytes,
            action=action,
            killed=killed,
            error_code=error_code,
            elapsed_seconds=elapsed_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - 证据记录失败不能影响执行本身
        logger.debug("[plugin] 配额证据记录失败：{}", str(exc)[:120])


def _record_refusal(spec: PluginQuotaSpec, *, detail: str = "") -> None:
    try:
        from app.services.plugins.boundary import record_refused

        record_refused(spec.plugin_id, error_code=PLUGIN_QUOTA_NOT_ENFORCED, detail=detail)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[plugin] 配额拒绝记录失败：{}", str(exc)[:120])


# ── 执行结果 ──────────────────────────────────────────────


@dataclass(slots=True)
class PluginWorkerOutcome:
    """一次插件 Worker 执行的配额结论（``action`` 取自 ``QuotaAction``）。"""

    ok: bool
    action: str = plugin_lifecycle.QuotaAction.NONE.value
    error_code: str = ""
    content: str = ""
    result_ref: dict[str, Any] | None = None
    output_bytes: int = 0
    elapsed_seconds: float = 0.0
    returncode: int | None = None
    killed: bool = False
    truncated: bool = False
    exceeded: tuple[str, ...] = ()
    plugin_id: str = ""
    plugin_version: str = ""
    decision: dict[str, Any] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.action in {
            plugin_lifecycle.QuotaAction.KILL_WORKER.value,
            plugin_lifecycle.QuotaAction.REFUSED.value,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": bool(self.ok),
            "action": self.action,
            "error_code": self.error_code,
            "result_ref": dict(self.result_ref or {}) or None,
            "output_bytes": int(self.output_bytes),
            "elapsed_seconds": round(float(self.elapsed_seconds), 6),
            "returncode": self.returncode,
            "killed": bool(self.killed),
            "truncated": bool(self.truncated),
            "exceeded": list(self.exceeded),
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "decision": dict(self.decision),
        }


@dataclass(slots=True)
class _RawRun:
    returncode: int | None
    output: bytes
    total_bytes: int
    timed_out: bool
    killed: bool
    truncated: bool
    started: float


def _preexec_limits(cpu_seconds: float, memory_mb: int):
    """POSIX 子进程资源限制（Windows 无 ``resource`` → 返回 ``None``）。"""
    if os.name != "posix":
        return None
    if not cpu_seconds and not memory_mb:
        return None

    def _apply() -> None:  # pragma: no cover - 仅 Linux/容器路径
        try:
            import resource

            if cpu_seconds:
                hard = max(1, int(round(cpu_seconds)))
                resource.setrlimit(resource.RLIMIT_CPU, (hard, hard + 1))
            if memory_mb:
                size = int(memory_mb) * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (size, size))
        except (ImportError, ValueError, OSError):
            pass

    return _apply


def run_process_bounded(
    argv: list[str],
    *,
    cwd: str | None,
    env: dict[str, str] | None,
    timeout: float,
    retain_bytes: int,
    cpu_seconds: float = 0.0,
    memory_mb: int = 0,
    stdin: bytes | None = None,
) -> _RawRun:
    """真执行子进程：超时 ``kill()``；只保留 ``retain_bytes`` 字节（0 = 不限）。

    ``total_bytes`` 是**真实**输出字节数（即使超过保留上限也照实报），因此
    ``ARTIFACT_REF`` 判定用的是真实体积，不是被截断后的体积。
    """
    started = time.monotonic()
    proc = subprocess.Popen(  # noqa: S603 - argv 由调用方显式给出，无 shell
        list(argv),
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        preexec_fn=_preexec_limits(cpu_seconds, memory_mb),
    )
    state = {"total": 0, "truncated": False}
    buffer = bytearray()

    def _drain() -> None:
        stream = proc.stdout
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(_READ_CHUNK)
                if not chunk:
                    break
                state["total"] += len(chunk)
                if retain_bytes <= 0:
                    buffer.extend(chunk)
                elif len(buffer) < retain_bytes:
                    buffer.extend(chunk[: retain_bytes - len(buffer)])
                if retain_bytes > 0 and state["total"] > retain_bytes:
                    state["truncated"] = True
        except (OSError, ValueError):  # 进程被杀时管道会先关闭
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    if stdin is not None:
        try:
            proc.stdin.write(stdin)  # type: ignore[union-attr]
            proc.stdin.close()  # type: ignore[union-attr]
        except (OSError, ValueError, BrokenPipeError):
            pass

    reader = threading.Thread(target=_drain, name="plugin-worker-reader", daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=max(0.05, float(timeout)))
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - 杀不掉的极端情况
            logger.warning("[plugin] Worker 进程 kill 后仍未退出：pid={}", proc.pid)
    reader.join(timeout=3)
    return _RawRun(
        returncode=proc.returncode,
        output=bytes(buffer),
        total_bytes=int(state["total"]),
        timed_out=timed_out,
        killed=bool(timed_out),
        truncated=bool(state["truncated"]),
        started=started,
    )


class PluginWorker:
    """插件 Worker 的配额执行边界（开关关闭时是**纯透传**，不做任何判定）。"""

    def __init__(
        self,
        *,
        spec: PluginQuotaSpec,
        artifact_sink: PluginArtifactSink | None = None,
        gate: PluginConcurrencyGate | None = None,
        owner: str = "",
        container: str = "",
        settings: Any = None,
    ) -> None:
        self._spec = spec
        self._sink = artifact_sink if artifact_sink is not None else GenericOutputArtifactSink()
        self._gate = gate if gate is not None else plugin_concurrency_gate
        self._owner = str(owner or "")
        self._container = str(container or "")
        self._settings = settings

    @property
    def spec(self) -> PluginQuotaSpec:
        return self._spec

    def enabled(self) -> bool:
        return bool(feature_enabled(FLAG, settings=self._settings))

    # ── 真进程 ────────────────────────────────────────────

    async def run_process(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        stdin: bytes | None = None,
        media_type: str = "text/plain",
        encoding: str = "utf-8",
    ) -> PluginWorkerOutcome:
        """在子进程里执行插件代码：超时真杀、输出真计量、并发真限流。"""
        enforced = self.enabled()
        hard = enforced and bool(self._spec.hard_limits)
        # 硬约束：**Manifest 配额就是权威**（不接受调用方放宽）。
        # 软约束（内置插件）：不按配额杀进程，只按调用方的既有墙钟跑，事后 WARN。
        if hard:
            limit = float(self._spec.quota.timeout_seconds)
        else:
            limit = float(timeout if timeout is not None else DEFAULT_WALL_SECONDS)
        retain = int(self._spec.quota.max_output_bytes) if enforced else 0
        if enforced:
            async with self._gate.hold(self._spec.plugin_id, self._spec.max_concurrency):
                raw = await asyncio.to_thread(
                    run_process_bounded,
                    list(argv),
                    cwd=cwd,
                    env=env,
                    timeout=limit,
                    retain_bytes=retain,
                    cpu_seconds=self._spec.cpu_seconds,
                    memory_mb=self._spec.quota.memory_mb,
                    stdin=stdin,
                )
        else:
            raw = await asyncio.to_thread(
                run_process_bounded,
                list(argv),
                cwd=cwd,
                env=env,
                timeout=limit,
                retain_bytes=0,
                stdin=stdin,
            )
        elapsed = max(0.0, time.monotonic() - raw.started)
        text = raw.output.decode(encoding, errors="replace")
        if not enforced:
            _record_evidence(
                self._spec,
                mode=TerminationMode.COOPERATIVE.value,
                output_bytes=raw.total_bytes,
                action=plugin_lifecycle.QuotaAction.NONE.value,
                killed=raw.killed,
                elapsed_seconds=elapsed,
            )
            return PluginWorkerOutcome(
                ok=raw.returncode == 0 and not raw.timed_out,
                content=text,
                output_bytes=raw.total_bytes,
                elapsed_seconds=elapsed,
                returncode=raw.returncode,
                killed=raw.killed,
                truncated=raw.truncated,
                plugin_id=self._spec.plugin_id,
                plugin_version=self._spec.plugin_version,
            )
        outcome = await self._decide(
            raw=raw, text=text, elapsed=elapsed, media_type=media_type
        )
        # 真执行证据由**执行边界**写：硬约束下这次跑的就是可强杀的独立进程。
        _record_evidence(
            self._spec,
            mode=TerminationMode.FORCED.value if hard else TerminationMode.COOPERATIVE.value,
            output_bytes=outcome.output_bytes,
            action=outcome.action,
            killed=outcome.killed,
            error_code=outcome.error_code,
            elapsed_seconds=outcome.elapsed_seconds,
        )
        return outcome

    # ── 进程内异步体（可协作取消） ─────────────────────────

    async def run(
        self,
        body: Any,
        *,
        media_type: str = "text/plain",
        reason: str = TerminationReason.TIMEOUT_NOTIFY.value,
    ) -> PluginWorkerOutcome:
        """在进程内执行插件体（**只允许**协作式取消的场景）。

        进程内**无法强杀**：``reason`` 只允许"超时通知 / 正常取消 / 用户停止"。
        需要硬终止的原因（CPU 超限、内存超限、死循环、失控子进程）一律**拒绝**
        （``REFUSED`` + ``PLUGIN_QUOTA_NOT_ENFORCED``），调用方必须改走
        :meth:`run_process`——绝不"悄悄按协作式跑"再报成功。
        """
        enforced = self.enabled()
        if enforced and requires_forced_termination(reason):
            outcome = PluginWorkerOutcome(
                ok=False,
                action=plugin_lifecycle.QuotaAction.REFUSED.value,
                error_code=PLUGIN_QUOTA_NOT_ENFORCED,
                plugin_id=self._spec.plugin_id,
                plugin_version=self._spec.plugin_version,
                decision={
                    "mode": TerminationMode.REFUSED.value,
                    "reason": str(reason),
                    "detail": "进程内只能协作取消：该原因需要强杀（run_process）",
                },
            )
            logger.warning(
                "[plugin] {} 拒绝进程内执行（reason={}）：需要硬终止，请走 run_process",
                self._spec.plugin_id, reason,
            )
            _record_refusal(self._spec, detail=f"in_process_cooperative_only:{reason}")
            return outcome
        started = time.monotonic()
        hard = enforced and bool(self._spec.hard_limits)
        timeout = float(self._spec.quota.timeout_seconds) if hard else None
        timed_out = False
        value: Any = None
        try:
            if enforced:
                async with self._gate.hold(self._spec.plugin_id, self._spec.max_concurrency):
                    value = await asyncio.wait_for(body(), timeout=timeout)
            else:
                value = await body()
        except (asyncio.TimeoutError, TimeoutError):
            timed_out = True
        elapsed = max(0.0, time.monotonic() - started)
        text = value if isinstance(value, str) else ("" if value is None else str(value))
        raw = _RawRun(
            returncode=None if timed_out else 0,
            output=text.encode("utf-8"),
            total_bytes=len(text.encode("utf-8")),
            timed_out=timed_out,
            killed=False,
            truncated=False,
            started=started,
        )
        if not enforced:
            _record_evidence(
                self._spec,
                mode=TerminationMode.COOPERATIVE.value,
                output_bytes=raw.total_bytes,
                action=plugin_lifecycle.QuotaAction.NONE.value,
                elapsed_seconds=elapsed,
            )
            return PluginWorkerOutcome(
                ok=not timed_out,
                content=text,
                output_bytes=raw.total_bytes,
                elapsed_seconds=elapsed,
                plugin_id=self._spec.plugin_id,
                plugin_version=self._spec.plugin_version,
            )
        outcome = await self._decide(raw=raw, text=text, elapsed=elapsed, media_type=media_type)
        _record_evidence(
            self._spec,
            mode=TerminationMode.COOPERATIVE.value,
            output_bytes=outcome.output_bytes,
            action=outcome.action,
            killed=outcome.killed,
            error_code=outcome.error_code,
            elapsed_seconds=outcome.elapsed_seconds,
        )
        return outcome

    # ── 判定 ──────────────────────────────────────────────

    async def _decide(
        self,
        *,
        raw: _RawRun,
        text: str,
        elapsed: float,
        media_type: str,
    ) -> PluginWorkerOutcome:
        decision = plugin_lifecycle.enforce_quota(
            self._spec.quota,
            output_bytes=raw.total_bytes,
            elapsed_seconds=elapsed,
            hard_limits=self._spec.hard_limits,
        )
        base = {
            "output_bytes": raw.total_bytes,
            "elapsed_seconds": elapsed,
            "returncode": raw.returncode,
            "killed": raw.killed,
            "truncated": raw.truncated,
            "exceeded": tuple(decision.exceeded),
            "plugin_id": self._spec.plugin_id,
            "plugin_version": self._spec.plugin_version,
            "decision": decision.as_dict(),
        }
        action = str(decision.action)
        if action == plugin_lifecycle.QuotaAction.KILL_WORKER.value:
            logger.warning(
                "[plugin] {} 超过资源上限（action=KILL_WORKER exceeded={} elapsed={:.3f}s bytes={}）",
                self._spec.plugin_id, list(decision.exceeded), elapsed, raw.total_bytes,
            )
            return PluginWorkerOutcome(
                ok=False,
                action=action,
                error_code=str(decision.error_code or "PLUGIN_RESOURCE_EXCEEDED"),
                **base,
            )
        if action == plugin_lifecycle.QuotaAction.ARTIFACT_REF.value:
            # 输出超限：正文进产物，事件只带引用（**不是失败**）。
            ref = await self._sink.put(
                plugin_id=self._spec.plugin_id,
                plugin_version=self._spec.plugin_version,
                content=text,
                output_bytes=raw.total_bytes,
                media_type=str(media_type),
                owner=self._owner,
                container=self._container,
            )
            return PluginWorkerOutcome(
                ok=raw.returncode == 0 and not raw.timed_out,
                action=action,
                content="",
                result_ref=dict(ref or {}),
                **base,
            )
        if action == plugin_lifecycle.QuotaAction.WARN.value:
            # 内置插件软约束：只告警，不杀、不转引用。
            logger.warning(
                "[plugin] 内置插件 {} 触及软约束（exceeded={}），仅告警不杀",
                self._spec.plugin_id, list(decision.exceeded),
            )
            return PluginWorkerOutcome(
                ok=raw.returncode == 0 and not raw.timed_out,
                action=action,
                error_code=str(decision.error_code),
                content=text,
                **base,
            )
        return PluginWorkerOutcome(
            ok=raw.returncode == 0 and not raw.timed_out,
            action=plugin_lifecycle.QuotaAction.NONE.value,
            content=text,
            **base,
        )


async def apply_quota_to_sandbox_result(
    result: Any,
    *,
    spec: PluginQuotaSpec,
    run_started: float,
    owner: str = "",
    container: str = "",
    sink: PluginArtifactSink | None = None,
    settings: Any = None,
) -> Any:
    """容器沙箱（docker exec）路径的配额后处理：同样是"超时杀 + 超限转引用"。

    只在开关打开时调用；理由：容器里的进程无法由本进程 ``kill()``（清理由沙箱的
    ``docker rm`` 负责），但**判定与产物化必须与子进程路径完全一致**。
    """
    if not feature_enabled(FLAG, settings=settings):
        return result
    elapsed = max(0.0, time.monotonic() - float(run_started))
    stdout = str(getattr(result, "stdout", "") or "")
    stderr = str(getattr(result, "stderr", "") or "")
    output_bytes = len(stdout.encode("utf-8")) + len(stderr.encode("utf-8"))
    timed_out = str(getattr(result, "status", "")) == "timeout"
    decision = plugin_lifecycle.enforce_quota(
        spec.quota,
        output_bytes=output_bytes,
        elapsed_seconds=max(elapsed, spec.quota.timeout_seconds + 0.001) if timed_out else elapsed,
        hard_limits=spec.hard_limits,
    )
    usage = dict(getattr(result, "resource_usage", {}) or {})
    usage["quota"] = decision.as_dict()
    usage["plugin_id"] = spec.plugin_id
    usage["plugin_version"] = spec.plugin_version
    usage["output_bytes"] = output_bytes
    result.resource_usage = usage
    _record_evidence(
        spec,
        mode=TerminationMode.FORCED.value if spec.hard_limits else TerminationMode.COOPERATIVE.value,
        output_bytes=output_bytes,
        action=str(decision.action),
        killed=bool(timed_out),
        error_code=str(decision.error_code or ""),
        elapsed_seconds=elapsed,
    )
    if decision.action == plugin_lifecycle.QuotaAction.KILL_WORKER.value:
        result.status = "timeout" if timed_out else str(getattr(result, "status", "error"))
        usage["error_code"] = str(decision.error_code or "PLUGIN_RESOURCE_EXCEEDED")
        result.error = f"插件 {spec.plugin_id} 超过资源上限：{', '.join(decision.exceeded)}"
        return result
    if decision.action == plugin_lifecycle.QuotaAction.ARTIFACT_REF.value:
        active = sink if sink is not None else GenericOutputArtifactSink()
        ref = await active.put(
            plugin_id=spec.plugin_id,
            plugin_version=spec.plugin_version,
            content=stdout,
            output_bytes=output_bytes,
            media_type="text/plain",
            owner=owner,
            container=container,
        )
        usage["result_ref"] = dict(ref or {})
        result.stdout = ""
        result.stderr = ""
    return result


__all__ = [
    "DEFAULT_WALL_SECONDS",
    "FLAG",
    "GenericOutputArtifactSink",
    "PluginArtifactSink",
    "PluginConcurrencyGate",
    "PluginQuotaSpec",
    "PluginWorker",
    "PluginWorkerOutcome",
    "apply_quota_to_sandbox_result",
    "plugin_concurrency_gate",
    "quota_spec_for",
    "run_process_bounded",
]
