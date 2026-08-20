"""Temporal Workflow：多智能体 DAG 任务编排（替换自建 execute_dag）.

⚠️ 本模块必须保持"轻"：Temporal 会在受限沙箱中导入它做校验，
因此不能 import loguru / pydantic / 配置模块等重依赖。
父包 app.agents 的 __init__ 保持为空（docstring 仅注释）。

设计约束：
  - Workflow 必须确定性：只使用 temporalio.workflow API 与 asyncio 任务等待；
    输入输出均为 JSON 安全 dict。
  - 任务树（nodes）作为 Workflow 输入；每个节点 = 一个 Activity
    （execute_node_activity）。React 重试与质检收敛在 Activity 内部，
    与 legacy dag.py 的语义保持一致。
  - 状态由 Temporal 管理：查询用 @workflow.query get_job；
    暂停/恢复/取消用 signal（取消可携带 keep_completed）。
"""

import asyncio
import re
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, CancelledError

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

JOB_RUNNING = "running"
JOB_PAUSED = "paused"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"
JOB_INTERRUPTED = "interrupted"
JOB_WAITING_APPROVAL = "waiting_approval"

_NODE_TIMEOUT_DEFAULT = 300
_NODE_MAX_RETRIES_DEFAULT = 2
_NODE_CONCURRENCY_DEFAULT = 2
MAX_REVIEW_LOOPS = 3  # 审查/测试打回后 writer 最多重写轮数（防死循环）


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
        self._job = payload
        self._paused = False
        self._cancel_requested = False
        self._keep_completed = True
        try:
            await self._execute()
        except CancelledError:
            self._finalize_cancelled()
            raise
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
        self._approvals[str(payload.get("node_id") or "")] = bool(payload.get("approved", True))

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
        }

    def _now(self) -> float:
        return workflow.now().timestamp()

    def _nodes(self) -> dict[str, dict]:
        return {n["id"]: n for n in self._job.get("nodes") or []}

    async def _execute(self) -> None:
        cfg = self._cfg()
        nodes = self._nodes()
        pending = set(nodes)
        completed: set[str] = set()
        running: dict[str, asyncio.Task] = {}

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

            ready = [
                nid
                for nid in pending
                if all(d in completed for d in nodes[nid].get("depends_on") or [])
            ]
            capacity = max(0, max(1, cfg["node_concurrency"]) - len(running))
            batch = ready[:capacity]
            if not batch and not running:
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
                await workflow.wait_condition(
                    lambda nid=nid: nid in self._approvals
                    or self._cancel_requested
                    or self._paused
                )
                if self._cancel_requested or self._paused:
                    runnable.append(nid)  # 走后续统一取消/暂停处理
                    continue
                if self._approvals.get(nid, False):
                    runnable.append(nid)
                else:
                    node["status"] = STATUS_SKIPPED
                    node["error"] = "用户拒绝审批"
                    node["completed_at"] = self._now()
                    pending.discard(nid)
                self._job["status"] = JOB_RUNNING

            for nid in runnable:
                pending.discard(nid)
                running[nid] = asyncio.create_task(self._run_node(nodes[nid]))

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

        await self._synthesize_final_answer()
        self._finalize()

    async def _synthesize_final_answer(self) -> None:
        """任务收尾：把用户请求 + 各已完成节点的产出合成最终答案（交付给用户）."""
        results = []
        for n in self._job.get("nodes") or []:
            if n.get("status") != STATUS_COMPLETED:
                continue
            r = n.get("result") or {}
            content = r.get("content") or r.get("output") or ""
            if content:
                results.append(
                    {
                        "agent": n.get("agent"),
                        "title": n.get("name") or n.get("agent"),
                        "content": str(content)[:30000],
                    }
                )
        if not results:
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
        except Exception:  # noqa: BLE001
            # 汇总失败不影响任务完成（节点结果仍在前端可见）
            pass

    async def _run_node(self, node: dict) -> None:
        cfg = self._cfg()
        node["status"] = STATUS_RUNNING
        node["started_at"] = self._now()
        node["error"] = None
        node["error_code"] = None
        node["retries"] = 0
        total_timeout = (cfg["node_max_retries"] + 1) * cfg["node_timeout_seconds"] + 60
        payload = {
            "job_id": self._job.get("job_id", ""),
            "user_id": self._job.get("user_id", ""),
            "user_role": self._job.get("user_role", "user"),
            "scene": self._job.get("scene", "office"),
            "node": node,
            "dependency_results": {
                dep_id: (self._nodes().get(dep_id) or {}).get("result")
                for dep_id in (node.get("depends_on") or [])
                if (self._nodes().get(dep_id) or {}).get("status") == STATUS_COMPLETED
            },
            "config": cfg,
        }
        try:
            out = await workflow.execute_activity(
                "execute_node_activity",
                payload,
                start_to_close_timeout=timedelta(seconds=total_timeout),
                # 1 次 Temporal 级重试：worker 崩溃/进程重启时自动重跑节点，
                # 避免后端中途重启导致任务直接失败（节点内部另有 React 重试兜底 LLM 错误）
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
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
        node["completed_at"] = self._now()

    def _finalize(self) -> None:
        statuses = [n.get("status") for n in (self._job.get("nodes") or [])]
        if self._job.get("status") not in (JOB_CANCELLED, JOB_INTERRUPTED, JOB_PAUSED):
            if statuses and all(s == STATUS_COMPLETED for s in statuses):
                self._job["status"] = JOB_COMPLETED
            elif any(s in (STATUS_FAILED, STATUS_SKIPPED) for s in statuses):
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
