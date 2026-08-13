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

_NODE_TIMEOUT_DEFAULT = 300
_NODE_MAX_RETRIES_DEFAULT = 2
_NODE_CONCURRENCY_DEFAULT = 2


@workflow.defn
class AgentDagWorkflow:
    """把任务树（DAG）按依赖顺序调度到执行 Activity 的 Workflow."""

    def __init__(self) -> None:
        self._job: dict = {}
        self._paused = False
        self._cancel_requested = False
        self._keep_completed = True

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

        while pending:
            if self._cancel_requested:
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
            if not ready:
                # 依赖链断裂：其余节点标记跳过
                for nid in pending:
                    nodes[nid]["status"] = STATUS_SKIPPED
                    nodes[nid]["error"] = "前置依赖失败"
                    nodes[nid]["completed_at"] = self._now()
                self._finalize()
                return

            batch = ready[: max(1, cfg["node_concurrency"])]
            node_tasks = {asyncio.create_task(self._run_node(nodes[nid])) for nid in batch}

            # 等待：本批全部结束 或 暂停/取消信号到达。
            # wait_condition 是确定性原语（活动完成 / 信号到达都会唤醒并重新求值），
            # 避免使用 asyncio.wait（沙箱会告警非确定性）。
            await workflow.wait_condition(
                lambda: all(t.done() for t in node_tasks)
                or self._cancel_requested
                or self._paused
            )

            if self._cancel_requested:
                for t in node_tasks:
                    t.cancel()
                await asyncio.gather(*node_tasks, return_exceptions=True)
                # 兜底：批内未完成的节点统一标记为中断（_finalize_cancelled
                # 再按 keep_completed 决定 CANCELLED / INTERRUPTED）
                for nid in batch:
                    n = nodes[nid]
                    if n.get("status") not in (STATUS_COMPLETED,):
                        n["status"] = STATUS_INTERRUPTED
                        n["error"] = "任务被用户终止"
                        n["completed_at"] = self._now()
                self._finalize_cancelled()
                return

            # 本批执行完毕（暂停时也等本批跑完，暂停只影响后续批次）
            await asyncio.gather(*node_tasks, return_exceptions=True)

            for nid in batch:
                pending.discard(nid)
                if nodes[nid].get("status") == STATUS_COMPLETED:
                    completed.add(nid)

        self._finalize()

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
            "scene": self._job.get("scene", "office"),
            "node": node,
            "config": cfg,
        }
        try:
            out = await workflow.execute_activity(
                "execute_node_activity",
                payload,
                start_to_close_timeout=timedelta(seconds=total_timeout),
                retry_policy=RetryPolicy(maximum_attempts=1),
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
        node["completed_at"] = self._now()

    def _finalize(self) -> None:
        statuses = [n.get("status") for n in (self._job.get("nodes") or [])]
        if self._job.get("status") not in (JOB_CANCELLED, JOB_INTERRUPTED, JOB_PAUSED):
            if statuses and all(s == STATUS_COMPLETED for s in statuses):
                self._job["status"] = JOB_COMPLETED
            elif any(s in (STATUS_FAILED, STATUS_SKIPPED) for s in statuses):
                self._job["status"] = JOB_FAILED
            else:
                self._job["status"] = JOB_RUNNING
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
