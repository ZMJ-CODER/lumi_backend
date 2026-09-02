"""Temporal Workflow：多智能体 DAG 任务编排（替换自建 execute_dag）.

本模块必须保持"轻"：Temporal 会在受限沙箱中导入它做校验，
因此不能 import loguru / pydantic / 配置模块等重依赖。
父包 app.agents 的 __init__ 保持为空（docstring 仅注释）。

设计约束：
  - Workflow 必须确定性：只使用 temporalio.workflow API 与 asyncio 任务等待；
    输入输出均为 JSON 安全 dict。
  - 任务树（nodes）作为 Workflow 输入；普通静态任务的每个节点直接调用
    Activity，长静态任务则通过 NodeExecutionWorkflow 子工作流包裹该 Activity。
    React 重试与质检收敛在 Activity 内部，
    与 legacy dag.py 的语义保持一致。
  - 状态由 Temporal 管理：查询用 @workflow.query get_job；
    暂停/恢复/取消用 signal（取消可携带 keep_completed）。
"""

import asyncio
import hashlib
import json
import re
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, CancelledError, ChildWorkflowError

# 与 app/agents/orchestration/models.py 状态枚举值保持一致（字符串字面量，
# 避免 workflow 沙箱内 import pydantic 模型）。
STATUS_PENDING = "pending"
STATUS_READY = "ready"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_RETRYING = "retrying"
STATUS_INTERRUPTED = "interrupted"
STATUS_CANCELLED = "cancelled"
STATUS_SKIPPED = "skipped"
STATUS_ESCALATED = "escalated"

JOB_RUNNING = "running"
JOB_PAUSED = "paused"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"
JOB_INTERRUPTED = "interrupted"
JOB_WAITING_APPROVAL = "waiting_approval"
JOB_WAITING_RESOURCES = "waiting_resources"

_NODE_TIMEOUT_DEFAULT = 300
_NODE_MAX_RETRIES_DEFAULT = 2
_NODE_CONCURRENCY_DEFAULT = 2
MAX_REVIEW_LOOPS = 3  # 审查/测试打回后 writer 最多重写轮数（防死循环）
_APPROVAL_TTL_SECONDS = 1800
_APPROVAL_ALLOWED_KEYS = {
    "success", "content", "output", "answer", "summary", "items", "results",
    "path", "doc_id", "project_id", "filename", "citations", "count", "status",
    "tool", "step_title", "metadata",
}
_CONTINUED_NODE_METADATA_KEYS = {
    # 这些字段是 Workflow 自己写入的运行态，不会改变冻结 NodeSpec 所授权的
    # agent、参数、资源声明或审批契约。
    "result_ref",
    "result_ref_error",
    "recovery",
    "temporal_child_attempt",
}


def _restore_frozen_nodes(payload: dict) -> list[dict]:
    """Rebuild node definitions from the frozen spec and restore runtime state.

    ``Continue-As-New`` carries only terminal state and result references.  It
    never trusts a mutable node definition from a previous run, so agent,
    params and dependency declarations still come solely from ``JobSpec``.
    """
    frozen = [dict(node) for node in ((payload.get("execution_spec") or {}).get("nodes") or [])]
    saved = {
        str(node.get("id") or ""): node
        for node in (payload.get("nodes") or [])
        if isinstance(node, dict)
    }
    runtime_fields = {
        "status", "result", "error", "error_code", "retries", "effect_status",
        "started_at", "completed_at",
    }
    restored: list[dict] = []
    for node in frozen:
        prior = saved.get(str(node.get("id") or ""))
        if prior:
            for field in runtime_fields:
                if field in prior:
                    node[field] = prior[field]
            prior_metadata = dict(prior.get("metadata") or {})
            node["metadata"] = {
                **dict(node.get("metadata") or {}),
                **{
                    key: value
                    for key, value in prior_metadata.items()
                    if key in _CONTINUED_NODE_METADATA_KEYS
                },
            }
        restored.append(node)
    return restored


def _compact_completed_node(node: dict) -> dict:
    """Drop completed result bodies before a long-DAG Workflow continues."""
    value = dict(node)
    if value.get("status") == STATUS_COMPLETED:
        value["result"] = None
    return value


@workflow.defn
class NodeExecutionWorkflow:
    """One deterministic node boundary for pure-read long static DAGs.

    The parent owns dependencies and lifecycle controls.  This child owns only
    a single Activity invocation and its retry envelope; external I/O remains
    entirely in the Activity.
    """

    @workflow.run
    async def run(self, payload: dict) -> dict:
        timeout = max(1, int(payload.get("total_timeout_seconds") or _NODE_TIMEOUT_DEFAULT))
        heartbeat_seconds = max(5, int(payload.get("heartbeat_seconds") or 15))
        activity_payload = dict(payload.get("activity_payload") or {})
        return await workflow.execute_activity(
            "execute_node_activity",
            activity_payload,
            start_to_close_timeout=timedelta(seconds=timeout),
            heartbeat_timeout=timedelta(seconds=max(heartbeat_seconds * 2, heartbeat_seconds + 5)),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )


def _approval_tool_binding(node: dict) -> tuple[str, dict]:
    """Return the concrete tool call represented by a static node.

    This mirrors the small set of statically allow-listed office workers.  It
    deliberately stays dependency-free so it can run inside the Workflow
    sandbox; unknown workers do not receive a synthetic approval credential.
    """
    params = node.get("params") or {}
    agent = str(node.get("agent") or "")
    if agent == "office_doc":
        mode = str(params.get("mode") or "read").lower()
        if mode == "read":
            return "office_doc_read", {"doc_id": str(params.get("doc_id") or "")}
        if mode == "edit":
            return "office_doc_edit", {
                "doc_id": str(params.get("doc_id") or ""),
                "instruction": str(params.get("instruction") or ""),
            }
        if mode == "analyze":
            return "office_doc_analyze", {
                "doc_id": str(params.get("doc_id") or ""),
                "instruction": str(params.get("instruction") or ""),
                "mode": str(params.get("analyze_mode") or "qa"),
            }
    if agent == "office_todo":
        return "todo_manager", {
            "action": str(params.get("action") or ""),
            "content": str(params.get("content") or ""),
            "due": str(params.get("due") or ""),
            "item_id": str(params.get("item_id") or ""),
        }
    if agent == "office_calendar":
        return "calendar_manager", {k: v for k, v in params.items() if k != "task"}
    tool = str(params.get("preferred_tool") or "")
    if tool:
        inputs = params.get("inputs")
        return tool, dict(inputs) if isinstance(inputs, dict) else {
            k: v for k, v in params.items() if k not in {"preferred_tool", "fallback_tools", "instruction"}
        }
    return "", {}


def _approval_fingerprint(tool: str, args: dict, upstream_sha256: str) -> str:
    raw = json.dumps(
        {"tool": tool, "args": args, "upstream_sha256": upstream_sha256},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sanitize_for_approval(value, depth: int = 0):
    """Small dependency-free mirror of context.sanitize_dependency_result."""
    if depth > 4:
        return "[已裁剪]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:6000] + ("…[已截断]" if len(value) > 6000 else "")
    if isinstance(value, list):
        return [_sanitize_for_approval(v, depth + 1) for v in value[:30]]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            name = str(key)
            if any(token in name.casefold() for token in ("password", "secret", "token", "api_key", "authorization", "cookie")):
                continue
            if depth == 0 and name not in _APPROVAL_ALLOWED_KEYS:
                continue
            out[name] = _sanitize_for_approval(item, depth + 1)
        return out
    return str(value)[:1000]


def _execution_spec_matches(payload: dict) -> bool:
    """Validate the immutable plan digest before the Workflow schedules it."""
    spec = payload.get("execution_spec")
    return _spec_fingerprint_matches(spec)


def _spec_fingerprint_matches(spec) -> bool:
    """Return whether a standalone JobSpec-like mapping has a valid digest."""
    if not isinstance(spec, dict):
        return False
    expected = str(spec.get("fingerprint") or "")
    if not expected:
        return False
    base = dict(spec)
    base.pop("fingerprint", None)
    raw = json.dumps(base, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest() == expected


def _review_rejection(node: dict) -> str | None:
    """审查/测试节点判定不合格时返回打回原因（供 DAG 反馈循环使用）."""
    agent = node.get("agent")
    result = node.get("result") or {}
    if agent == "code_reviewer" and result.get("approved") is False:
        issues = result.get("issues") or []
        feedback = str(result.get("feedback") or "")
        detail = "；".join(str(i) for i in issues[:5])
        if feedback and detail:
            return f"审查未通过：{detail}；{feedback}"
        return f"审查未通过：{detail or feedback or '代码不合要求'}"
    if agent == "code_tester" and result.get("tests_passed") is False:
        output = str(result.get("output") or result.get("error") or "")[:2000]
        # 第三层上下文：运行报错日志全量带回给 writer 分析
        return f"运行报错日志（测试未通过）：\n{output or '构建/测试失败'}"
    return None


def _writer_ancestors(node_id: str, nodes: dict, seen: set | None = None) -> list[str]:
    """沿 depends_on 依赖链向上找 writer/code 祖先节点 id（审查节点常隔着 tester 依赖 writer）."""
    seen = seen or set()
    out: list[str] = []
    for dep_id in nodes.get(node_id, {}).get("depends_on") or []:
        if dep_id in seen:
            continue
        seen.add(dep_id)
        dep = nodes.get(dep_id)
        if dep and dep.get("agent") in ("code_writer", "code"):
            out.append(dep_id)
        out.extend(_writer_ancestors(dep_id, nodes, seen))
    return out


def _norm_path(p: str) -> str:
    p = str(p or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _extract_failed_paths(text: str) -> set[str]:
    """从打回反馈（静态检查/测试输出）中提取带行号的出错文件路径."""
    if not text:
        return set()
    out: set[str] = set()
    for m in re.finditer(
        r"([A-Za-z0-9_./\\-]+\.(?:vue|ts|tsx|js|jsx|py|go|json|css|scss|md))\s*:\s*\d+",
        text,
    ):
        out.add(_norm_path(m.group(1)))
    # 导入扫描等输出没有行号（如 src/foo.js: 找不到相对导入）
    for m in re.finditer(r"([A-Za-z0-9_./\\-]+\.(?:vue|ts|tsx|js|jsx))\s*:", text):
        out.add(_norm_path(m.group(1)))
    return {p for p in out if p}


def _node_file(node: dict | None) -> str:
    """writer 节点负责的文件（result.path 或 params.target_file）."""
    if not node:
        return ""
    res = node.get("result") or {}
    return _norm_path(
        res.get("path") or (node.get("params") or {}).get("target_file") or ""
    )


@workflow.defn
class AgentDagWorkflow:
    """把任务树（DAG）按依赖顺序调度到执行 Activity 的 Workflow."""

    def __init__(self) -> None:
        self._job: dict = {}
        self._paused = False
        self._cancel_requested = False
        self._keep_completed = True
        self._approvals: dict[str, bool] = {}

    @workflow.run
    async def run(self, payload: dict) -> dict:
        if not _execution_spec_matches(payload):
            return {
                "job_id": str(payload.get("job_id") or ""),
                "status": JOB_FAILED,
                "error": "Temporal 执行规格缺失或指纹校验失败",
                "error_code": "INVALID_EXECUTION_SPEC",
                "nodes": payload.get("nodes") or [],
            }
        # Scheduling always starts from the frozen NodeSpec list. Mutable
        # Redis/API snapshots are presentation state and must not alter what a
        # persisted Temporal run is authorized to execute. A continued long
        # DAG only restores terminal state and result references from ``nodes``.
        payload = dict(payload)
        payload["nodes"] = _restore_frozen_nodes(payload)
        self._job = payload
        self._paused = False
        self._cancel_requested = False
        self._keep_completed = True
        try:
            await self._execute()
        except CancelledError:
            self._finalize_cancelled()
            raise
        except Exception as exc:
            # A Workflow-level defect must still converge to a queryable
            # terminal snapshot.  Without this, the API falls back to the
            # initial Redis RUNNING record and the desktop UI remains on the
            # stop button forever after an internal Activity/workflow error.
            message = str(exc) or "办公任务执行异常"
            for node in self._job.get("nodes") or []:
                if node.get("status") in (STATUS_PENDING, STATUS_READY, STATUS_RUNNING, STATUS_RETRYING):
                    node["status"] = STATUS_FAILED
                    node["error"] = "任务因执行异常自动停止"
                    node["completed_at"] = self._now()
            self._job["status"] = JOB_FAILED
            self._job["error"] = message[:500]
            self._job["updated_at"] = self._now()
        # Credential and planning bridges are outside Temporal history. Normal
        # completion should clear them; cancelled runs retain the TTL fallback.
        await self._cleanup_terminal_secrets()
        return self._job

    # ── 外部控制：暂停 / 恢复 / 取消 ──────────────────────────

    @workflow.signal
    async def pause(self) -> None:
        self._paused = True

    @workflow.signal
    async def resume(self) -> None:
        self._paused = False

    @workflow.signal
    async def cancel_request(self, keep_completed: bool = True) -> None:
        self._cancel_requested = True
        self._keep_completed = bool(keep_completed)

    @workflow.signal
    async def approve_task(self, payload) -> None:
        """人工审批结果：approved=True 执行该节点，False 跳过."""
        payload = payload or {}
        node_id = str(payload.get("node_id") or "")
        approved = bool(payload.get("approved", True))
        self._approvals[node_id] = approved
        # Keep the workflow snapshot self-contained.  The API persists the
        # same credentials in its repository, while this mutation makes the
        # Temporal Activity receive them even when the repository lags behind
        # a query/signal round trip.
        node = next((n for n in self._job.get("nodes") or [] if str(n.get("id")) == node_id), None)
        if node is not None and approved:
            tool, args = _approval_tool_binding(node)
            if tool:
                upstream = self._upstream_hash(node)
                metadata = dict(node.get("metadata") or {})
                fingerprint = _approval_fingerprint(tool, args, upstream)
                metadata.pop("awaiting_approval", None)
                metadata["approval_tool"] = tool
                metadata["approval_fingerprint"] = fingerprint
                metadata["confirmed_tools"] = sorted({*(str(v) for v in metadata.get("confirmed_tools") or []), tool} - {""})
                metadata["confirmed_tool_calls"] = sorted({*(str(v) for v in metadata.get("confirmed_tool_calls") or []), fingerprint} - {""})
                node["metadata"] = metadata

    @workflow.query
    def get_job(self) -> dict:
        return self._job

    # ── 内部实现 ────────────────────────────────────────────

    def _cfg(self) -> dict:
        cfg = self._job.get("config") or {}
        return {
            "node_timeout_seconds": int(cfg.get("node_timeout_seconds") or _NODE_TIMEOUT_DEFAULT),
            "node_max_retries": int(cfg.get("node_max_retries") or _NODE_MAX_RETRIES_DEFAULT),
            "node_concurrency": int(cfg.get("node_concurrency") or _NODE_CONCURRENCY_DEFAULT),
            "static_max_replans": int(cfg.get("static_max_replans") or 1),
            "long_dag": bool(cfg.get("long_dag", False)),
            "use_node_child_workflows": bool(cfg.get("use_node_child_workflows", False)),
            "continue_as_new_after_nodes": max(1, int(cfg.get("continue_as_new_after_nodes") or 20)),
            "activity_heartbeat_seconds": max(5, int(cfg.get("activity_heartbeat_seconds") or 15)),
        }

    def _now(self) -> float:
        return workflow.now().timestamp()

    def _nodes(self) -> dict[str, dict]:
        return {n["id"]: n for n in self._job.get("nodes") or []}

    def _upstream_hash(self, node: dict) -> str:
        deps = self._nodes()
        values = {
            str(dep_id): _sanitize_for_approval((deps.get(dep_id) or {}).get("result") or {})
            for dep_id in (node.get("depends_on") or [])
            if (deps.get(dep_id) or {}).get("status") == STATUS_COMPLETED
        }
        if not values:
            return ""
        raw = json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _prepare_approval(self, node: dict) -> None:
        """Expose the exact approval contract before waiting for a signal."""
        tool, args = _approval_tool_binding(node)
        if not tool:
            return
        upstream = self._upstream_hash(node)
        metadata = dict(node.get("metadata") or {})
        metadata["awaiting_approval"] = True
        metadata["approval_tool"] = tool
        metadata["approval_fingerprint"] = _approval_fingerprint(tool, args, upstream)
        metadata["approval_upstream_sha256"] = upstream
        metadata["approval_expires_at"] = self._now() + _APPROVAL_TTL_SECONDS
        node["metadata"] = metadata

    async def _execute(self) -> None:
        cfg = self._cfg()
        nodes = self._nodes()
        pending = {
            node_id for node_id, node in nodes.items()
            if node.get("status") not in {STATUS_COMPLETED, STATUS_FAILED, STATUS_ESCALATED, STATUS_SKIPPED, STATUS_CANCELLED, STATUS_INTERRUPTED}
        }
        completed: set[str] = {
            node_id for node_id, node in nodes.items()
            if node.get("status") == STATUS_COMPLETED
        }
        running: dict[str, asyncio.Task] = {}
        completed_since_continue = 0

        while pending or running:
            if self._cancel_requested:
                for task in running.values():
                    task.cancel()
                await asyncio.gather(*running.values(), return_exceptions=True)
                self._finalize_cancelled()
                return
            if self._paused:
                await workflow.wait_condition(
                    lambda: (not self._paused) or self._cancel_requested
                )
                continue

            waiting_resources = [
                nid for nid in pending
                if bool((nodes[nid].get("metadata") or {}).get("waiting_resources"))
            ]
            ready = [
                nid
                for nid in pending
                if nid not in waiting_resources
                if all(d in completed for d in nodes[nid].get("depends_on") or [])
            ]
            capacity = max(0, max(1, cfg["node_concurrency"]) - len(running))
            batch = ready[:capacity]
            if not batch and not running:
                if waiting_resources:
                    self._job["status"] = JOB_WAITING_RESOURCES
                    # Activity returns this state before it acquires a channel
                    # lease or writes an effect intent. A timer is a
                    # deterministic Temporal backoff.
                    await workflow.sleep(timedelta(seconds=5))
                    for nid in waiting_resources:
                        metadata = dict(nodes[nid].get("metadata") or {})
                        metadata.pop("waiting_resources", None)
                        nodes[nid]["metadata"] = metadata
                    self._job["status"] = JOB_RUNNING
                    continue
                # 依赖链断裂：其余节点标记跳过
                for nid in pending:
                    nodes[nid]["status"] = STATUS_SKIPPED
                    nodes[nid]["error"] = "前置依赖失败"
                    nodes[nid]["completed_at"] = self._now()
                self._finalize()
                return

            # 审批门控（Human-in-the-Loop）：高风险节点先暂停，等待人工审批后再执行
            runnable = []
            for nid in batch:
                node = nodes[nid]
                if node.get("approval") is not True:
                    runnable.append(nid)
                    continue
                self._job["status"] = JOB_WAITING_APPROVAL
                node["status"] = STATUS_RUNNING
                node["error"] = None
                self._prepare_approval(node)
                await workflow.wait_condition(
                    lambda nid=nid: nid in self._approvals
                    or self._cancel_requested
                    or self._paused
                )
                if self._cancel_requested or self._paused:
                    runnable.append(nid)  # 走后续统一取消/暂停处理
                    continue
                if self._approvals.get(nid, False):
                    # Defensive fallback for signals received before the
                    # approval metadata was materialized.
                    metadata = dict(node.get("metadata") or {})
                    if not metadata.get("confirmed_tool_calls"):
                        tool, args = _approval_tool_binding(node)
                        if tool:
                            fp = _approval_fingerprint(tool, args, self._upstream_hash(node))
                            metadata["confirmed_tools"] = sorted({tool})
                            metadata["confirmed_tool_calls"] = sorted({fp})
                            metadata["approval_upstream_sha256"] = self._upstream_hash(node)
                            node["metadata"] = metadata
                    runnable.append(nid)
                else:
                    node["status"] = STATUS_SKIPPED
                    node["error"] = "用户拒绝审批"
                    node["completed_at"] = self._now()
                    pending.discard(nid)
                self._job["status"] = JOB_RUNNING

            for nid in runnable:
                pending.discard(nid)
                running[nid] = asyncio.create_task(self._run_node(nodes[nid], cfg))

            if not running:
                continue

            # 等待：任一步骤结束即推进它的后继；无依赖的同批步骤继续独立运行。
            # Workflow 使用 wait_condition 保持确定性。
            await workflow.wait_condition(
                lambda running=running: any(t.done() for t in running.values())
                or self._cancel_requested
                or self._paused
            )

            if self._cancel_requested:
                for t in running.values():
                    t.cancel()
                await asyncio.gather(*running.values(), return_exceptions=True)
                # 兜底：批内未完成的节点统一标记为中断（_finalize_cancelled
                # 再按 keep_completed 决定 CANCELLED / INTERRUPTED）
                for nid in list(running):
                    n = nodes[nid]
                    if n.get("status") not in (STATUS_COMPLETED,):
                        n["status"] = STATUS_INTERRUPTED
                        n["error"] = "任务被用户终止"
                        n["completed_at"] = self._now()
                self._finalize_cancelled()
                return

            finished = [nid for nid, task in running.items() if task.done()]
            for nid in finished:
                task = running.pop(nid)
                await asyncio.gather(task, return_exceptions=True)
                if nodes[nid].get("status") == STATUS_COMPLETED:
                    completed.add(nid)
                    completed_since_continue += 1
                elif nodes[nid].get("status") == JOB_WAITING_RESOURCES:
                    nodes[nid]["status"] = STATUS_PENDING
                    metadata = dict(nodes[nid].get("metadata") or {})
                    metadata["waiting_resources"] = True
                    nodes[nid]["metadata"] = metadata
                    pending.add(nid)
                    self._job["status"] = JOB_WAITING_RESOURCES

            # 审查/测试打回：不合格代码返回给上游 writer 重写（带反馈，最多 MAX_REVIEW_LOOPS 轮）
            for nid in finished:
                node = nodes[nid]
                rejection = _review_rejection(node)
                if not rejection:
                    continue
                writer_ids = list(dict.fromkeys(_writer_ancestors(node.get("id"), nodes)))
                # R9：打回只重跑报错文件对应的 writer，避免全部 writer 祖先重跑
                failed_paths = _extract_failed_paths(rejection)
                if failed_paths:
                    targeted = [
                        w
                        for w in writer_ids
                        if _node_file(nodes.get(w))
                        and any(
                            fp == _node_file(nodes.get(w))
                            or fp.endswith("/" + _node_file(nodes.get(w)))
                            for fp in failed_paths
                        )
                    ]
                    if targeted:
                        writer_ids = targeted
                for dep_id in writer_ids:
                    dep = nodes.get(dep_id)
                    if dep.get("status") != STATUS_COMPLETED:
                        continue
                    loops = int((dep.get("metadata") or {}).get("review_loops", 0))
                    if loops >= MAX_REVIEW_LOOPS:
                        continue
                    meta = dict(dep.get("metadata") or {})
                    meta["review_loops"] = loops + 1
                    meta["review_feedback"] = rejection
                    dep["metadata"] = meta
                    dep_params = dict(dep.get("params") or {})
                    dep_params["instruction"] = (
                        str(dep_params.get("instruction") or "")
                        + f"\n\n【第{loops + 1}轮审查意见】{rejection}"
                    )
                    dep["params"] = dep_params
                    dep["status"] = STATUS_PENDING
                    dep["result"] = None
                    dep["error"] = None
                    dep["error_code"] = None
                    pending.add(dep_id)
                    completed.discard(dep_id)
                    # 该 writer 的直接下游（tester/reviewer）一并重跑，验证新代码
                    for other in nodes.values():
                        if (
                            dep_id in (other.get("depends_on") or [])
                            and other.get("status") == STATUS_COMPLETED
                        ):
                            other["status"] = STATUS_PENDING
                            other["result"] = None
                            other["error"] = None
                            other["error_code"] = None
                            pending.add(other.get("id"))
                            completed.discard(other.get("id"))
                # 打回节点自身也重跑（重新审查/测试新代码）
                node["status"] = STATUS_PENDING
                node["result"] = None
                node["error"] = None
                node["error_code"] = None
                pending.add(node.get("id"))
                completed.discard(node.get("id"))

            # A read-only replacement is created by an Activity.  Waiting for
            # the current window to settle gives it a stable completed prefix
            # and prevents an old branch from racing newly mounted nodes.
            if not running and await self._maybe_mount_static_replan(nodes, pending, completed, cfg):
                nodes = self._nodes()
                continue

            # Only pure-read long DAGs reach this branch. Cut a new Workflow
            # Run at a settled boundary so no child is left in flight; keep
            # completed result references, not model output bodies, in input.
            if (
                cfg["long_dag"]
                and not running
                and pending
                and completed_since_continue >= cfg["continue_as_new_after_nodes"]
                and self._can_continue_as_new()
            ):
                self._continue_as_new(completed_since_continue)

        await self._synthesize_final_answer()
        self._finalize()

    async def _maybe_mount_static_replan(
        self,
        nodes: dict[str, dict],
        pending: set[str],
        completed: set[str],
        cfg: dict,
    ) -> bool:
        """Request, validate, and mount one Activity-produced pure-read plan."""
        if int((self._job.get("routing") or {}).get("replan_count") or 0) >= cfg["static_max_replans"]:
            return False
        failed = [
            node for node in nodes.values()
            if node.get("status") in {STATUS_FAILED, STATUS_ESCALATED}
            and bool((node.get("metadata") or {}).get("recovery", {}).get("replan_required"))
        ]
        if not failed:
            return False
        execution_spec = self._job.get("execution_spec") or {}
        if any(
            bool(node.get("approval")) or bool(node.get("idempotency_key"))
            for node in execution_spec.get("nodes") or []
        ):
            return False
        try:
            out = await workflow.execute_activity(
                "replan_static_job_activity",
                {
                    "job_id": self._job.get("job_id", ""),
                    "user_role": self._job.get("user_role", "user"),
                    "execution_spec": execution_spec,
                    "nodes": list(nodes.values()),
                },
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except Exception as exc:
            self._job.setdefault("routing", {})["temporal_replan_error"] = str(exc)[:300]
            return False
        if not isinstance(out, dict) or not out.get("allowed"):
            self._job.setdefault("routing", {})["temporal_replan_blocked"] = str(
                (out or {}).get("reason") or "replan_unavailable"
            )[:160]
            return False
        new_spec = out.get("execution_spec")
        if not _spec_fingerprint_matches(new_spec):
            self._job.setdefault("routing", {})["temporal_replan_blocked"] = "invalid_replacement_spec"
            return False
        replacement_ids = {str(value) for value in out.get("replacement_node_ids") or []}
        replacement = [
            dict(node) for node in (new_spec.get("nodes") or [])
            if str(node.get("id")) in replacement_ids
        ]
        if not replacement or len(replacement) != len(replacement_ids):
            self._job.setdefault("routing", {})["temporal_replan_blocked"] = "replacement_nodes_missing"
            return False
        for node in nodes.values():
            if node.get("status") not in {STATUS_COMPLETED, STATUS_CANCELLED, STATUS_INTERRUPTED, STATUS_SKIPPED}:
                node["status"] = STATUS_SKIPPED
                node["error"] = "已由 Temporal 重规划子图接管"
                node["completed_at"] = self._now()
                pending.discard(str(node.get("id")))
        self._job["nodes"].extend(replacement)
        self._job["execution_spec"] = new_spec
        routing = dict(self._job.get("routing") or {})
        routing["replan_count"] = int((new_spec.get("routing") or {}).get("replan_count") or 1)
        routing["plan_revision"] = int(out.get("revision") or routing.get("plan_revision") or 1)
        routing["temporal_replan"] = {
            "node_ids": sorted(replacement_ids),
            "plan_text": str(out.get("plan_text") or "")[:1000],
        }
        self._job["routing"] = routing
        pending.update(replacement_ids)
        return True

    async def _synthesize_final_answer(self) -> None:
        """任务收尾：把用户请求 + 各已完成节点的产出合成最终答案（交付给用户）."""
        results = []
        for n in self._job.get("nodes") or []:
            if n.get("status") != STATUS_COMPLETED:
                continue
            r = n.get("result") or {}
            content = r.get("content") or r.get("output") or ""
            result_ref = (n.get("metadata") or {}).get("result_ref")
            if content or result_ref:
                results.append(
                    {
                        "agent": n.get("agent"),
                        "title": n.get("name") or n.get("agent"),
                        "content": str(content)[:30000],
                        "result_ref": result_ref if not content else None,
                    }
                )
        if not results:
            return
        # A single text-producing node is already the final delivery.  Asking
        # another model to "summarize" it doubles latency and can make an
        # otherwise streamed office answer appear to hang after it is written.
        # Artifact nodes keep their own delivery contract too; the API renders
        # the returned file metadata rather than forcing a prose synthesis.
        if len(results) == 1 and results[0].get("content"):
            self._job["result"] = {"final_answer": str(results[0]["content"])}
            return
        try:
            out = await workflow.execute_activity(
                "synthesize_final_answer_activity",
                {
                    "user_id": self._job.get("user_id", ""),
                    "job_id": self._job.get("job_id", ""),
                    "request": self._job.get("request", ""),
                    "nodes": results,
                },
                start_to_close_timeout=timedelta(seconds=120),
            )
            if out and out.get("final_answer"):
                self._job["result"] = {
                    "final_answer": str(out["final_answer"]),
                    "source_nodes": len(results),
                }
        except Exception as exc:  # noqa: BLE001
            from app.agents.skills.recovery import classify_model_error

            code, message = classify_model_error(exc)
            if code.startswith("MODEL_"):
                self._job["status"] = JOB_FAILED
                self._job["error"] = message[:500]
                self._job["error_code"] = code
            # Non-provider formatting failures remain non-fatal because the
            # completed node results are still directly deliverable.

    def _continue_as_new(self, completed_since_continue: int) -> None:
        """Continue a long pure-read job with compact terminal state only."""
        next_payload = dict(self._job)
        # The frozen specification is the authorization source.  Do not send
        # the mutable presentation ``nodes`` list as an alternate plan.
        next_payload["nodes"] = [
            _compact_completed_node(node)
            for node in (self._job.get("nodes") or [])
        ]
        routing = dict(next_payload.get("routing") or {})
        routing["temporal_continue_as_new_count"] = int(routing.get("temporal_continue_as_new_count") or 0) + 1
        routing["temporal_completed_since_continue"] = int(completed_since_continue)
        next_payload["routing"] = routing
        workflow.continue_as_new(next_payload)

    def _can_continue_as_new(self) -> bool:
        """Never discard a completed body until its verified reference exists."""
        return all(
            node.get("status") != STATUS_COMPLETED
            or bool((node.get("metadata") or {}).get("result_ref"))
            or not node.get("result")
            for node in (self._job.get("nodes") or [])
        )

    async def _cleanup_terminal_secrets(self) -> None:
        try:
            await workflow.execute_activity(
                "cleanup_job_secrets_activity",
                str(self._job.get("job_id") or ""),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except Exception:
            # Redis TTL remains the final safety backstop. A cleanup issue must
            # not alter an otherwise completed user-facing result.
            pass

    async def _run_node(self, node: dict, cfg: dict | None = None) -> None:
        cfg = cfg or self._cfg()
        metadata = dict(node.get("metadata") or {})
        child_attempt = int(metadata.get("temporal_child_attempt") or 0) + 1
        metadata["temporal_child_attempt"] = child_attempt
        node["metadata"] = metadata
        node["status"] = STATUS_RUNNING
        node["started_at"] = self._now()
        node["error"] = None
        node["error_code"] = None
        node["retries"] = 0
        total_timeout = (cfg["node_max_retries"] + 1) * cfg["node_timeout_seconds"] + 60
        dependency_results = {
            dep_id: (self._nodes().get(dep_id) or {}).get("result")
            for dep_id in (node.get("depends_on") or [])
            if (self._nodes().get(dep_id) or {}).get("status") == STATUS_COMPLETED
            and (self._nodes().get(dep_id) or {}).get("result")
        }
        dependency_refs = {
            dep_id: ((self._nodes().get(dep_id) or {}).get("metadata") or {}).get("result_ref")
            for dep_id in (node.get("depends_on") or [])
            if (self._nodes().get(dep_id) or {}).get("status") == STATUS_COMPLETED
            and not (self._nodes().get(dep_id) or {}).get("result")
        }
        if dependency_refs:
            metadata = dict(node.get("metadata") or {})
            metadata["temporal_dependency_refs"] = dependency_refs
            node["metadata"] = metadata
        payload = {
            "job_id": self._job.get("job_id", ""),
            "user_id": self._job.get("user_id", ""),
            "user_role": self._job.get("user_role", "user"),
            "scene": self._job.get("scene", "office"),
            "user_request": self._job.get("request", ""),
            "node": node,
            "dependency_results": dependency_results,
            "config": cfg,
        }
        try:
            if cfg["long_dag"] and cfg["use_node_child_workflows"]:
                out = await workflow.execute_child_workflow(
                    NodeExecutionWorkflow.run,
                    {
                        "activity_payload": payload,
                        "total_timeout_seconds": total_timeout,
                        "heartbeat_seconds": cfg["activity_heartbeat_seconds"],
                    },
                    # Continue-As-New preserves the Workflow ID, so include
                    # the chain-local run marker to keep child IDs unique.
                    id=(
                        f"{self._job.get('job_id', '')}:{node.get('id', '')}:"
                        f"{int((self._job.get('routing') or {}).get('temporal_continue_as_new_count') or 0)}:"
                        f"{child_attempt}"
                    ),
                )
            else:
                out = await workflow.execute_activity(
                    "execute_node_activity",
                    payload,
                    start_to_close_timeout=timedelta(seconds=total_timeout),
                    heartbeat_timeout=timedelta(
                        seconds=max(
                            cfg["activity_heartbeat_seconds"] * 2,
                            cfg["activity_heartbeat_seconds"] + 5,
                        )
                    ),
                    # 1 次 Temporal 级重试：worker 崩溃/进程重启时自动重跑节点，
                    # 避免后端中途重启导致任务直接失败（节点内部另有 React 重试兜底 LLM 错误）
                    retry_policy=RetryPolicy(maximum_attempts=2),
                )
        except (ActivityError, ChildWorkflowError) as exc:
            out = {
                "status": STATUS_FAILED,
                "result": None,
                "error": str(getattr(exc, "cause", None) or exc),
                "error_code": "EXEC_ERROR",
                "retries": node.get("retries", 0),
            }
        except CancelledError:
            node["status"] = STATUS_INTERRUPTED
            node["completed_at"] = self._now()
            node["error"] = "任务被用户终止"
            raise
        out = out or {}
        node["status"] = out.get("status") or STATUS_FAILED
        node["result"] = out.get("result")
        node["error"] = out.get("error")
        node["error_code"] = out.get("error_code")
        node["retries"] = int(out.get("retries") or 0)
        if isinstance(out.get("recovery"), dict):
            metadata = dict(node.get("metadata") or {})
            metadata["recovery"] = out["recovery"]
            node["metadata"] = metadata
        if out.get("effect_status"):
            node["effect_status"] = out.get("effect_status")
        if node["status"] == STATUS_COMPLETED and cfg["long_dag"]:
            # Persist a body-free reference in an Activity before a later
            # Continue-As-New cuts the history. The parent retains the body
            # only for the current scheduling window.
            try:
                ref = await workflow.execute_activity(
                    "persist_node_result_ref_activity",
                    {
                        "user_id": self._job.get("user_id", ""),
                        "result": node.get("result"),
                    },
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=2),
                )
                if isinstance(ref, dict) and ref.get("id") and ref.get("sha256"):
                    metadata = dict(node.get("metadata") or {})
                    metadata["result_ref"] = ref
                    node["metadata"] = metadata
            except ActivityError:
                # A result body remains available in the current run. Ref
                # persistence failure blocks only the next history cut.
                metadata = dict(node.get("metadata") or {})
                metadata["result_ref_error"] = "persist_failed"
                node["metadata"] = metadata
        node["completed_at"] = self._now()

    def _finalize(self) -> None:
        statuses = [n.get("status") for n in (self._job.get("nodes") or [])]
        if self._job.get("status") not in (
            JOB_CANCELLED, JOB_INTERRUPTED, JOB_PAUSED, JOB_WAITING_RESOURCES
        ):
            if statuses and all(s == STATUS_COMPLETED for s in statuses):
                self._job["status"] = JOB_COMPLETED
            elif any(s in (STATUS_FAILED, STATUS_SKIPPED, STATUS_ESCALATED) for s in statuses):
                self._job["status"] = JOB_FAILED
            elif not statuses:
                # 空任务树（防御性兜底）：有结果视为完成，无结果视为失败
                self._job["status"] = (
                    JOB_COMPLETED if self._job.get("result") else JOB_FAILED
                )
            else:
                self._job["status"] = JOB_RUNNING
        if self._job.get("status") == JOB_FAILED and not self._job.get("error"):
            failed = next(
                (
                    n for n in (self._job.get("nodes") or [])
                    if n.get("status") == STATUS_FAILED and n.get("error")
                ),
                None,
            )
            if failed:
                self._job["error"] = str(failed.get("error"))
        self._job["updated_at"] = self._now()

    def _finalize_cancelled(self) -> None:
        for n in self._job.get("nodes") or []:
            status = n.get("status")
            if status in (STATUS_PENDING, STATUS_READY, STATUS_RETRYING):
                n["status"] = STATUS_CANCELLED
                n["error"] = "任务被用户终止"
            elif not self._keep_completed and status in (STATUS_RUNNING, STATUS_INTERRUPTED):
                n["status"] = STATUS_CANCELLED
                n["error"] = "任务被用户终止"
        self._job["status"] = JOB_CANCELLED
        self._job["updated_at"] = self._now()
