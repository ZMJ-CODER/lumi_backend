"""Execution-runtime adapter for the orchestration facade.

This module owns backend selection and Temporal submission details.  It does
not decide plans or execute nodes; those remain separate compiler/executor
concerns.
"""

from __future__ import annotations

import time

from loguru import logger

from app.agents.orchestration.models import Job
from app.agents.orchestration.state import StateStore
from app.core.config import settings


class RuntimeGateway:
    def __init__(self, *, store: StateStore, temporal_mode: bool):
        self.store = store
        self.temporal_mode = temporal_mode
        self.temporal_available = False
        self.temporal_probe_at = 0.0
        self.temporal_unavailable_until = 0.0

    async def probe_temporal(self) -> bool:
        if not self.temporal_mode:
            return False
        if self.temporal_available:
            return True
        now = time.monotonic()
        if now < self.temporal_unavailable_until:
            return False
        if now - self.temporal_probe_at < 30:
            return False
        self.temporal_probe_at = now
        try:
            from app.agents.orchestration.temporal.client import get_temporal_client

            await get_temporal_client()
            self.temporal_available = True
            self.temporal_unavailable_until = 0.0
            logger.info("Temporal 已连接: {}", settings.TEMPORAL_ADDRESS)
        except Exception as exc:  # noqa: BLE001
            self.temporal_unavailable_until = now + 300
            logger.warning("Temporal 不可用（{}），多智能体任务回退自建 DAG", exc)
        return self.temporal_available

    async def submit_static(self, job: Job, llm_api_key: str | None, llm_config: dict | None = None) -> None:
        """Submit a safe, static DAG to the external Temporal worker."""
        from app.agents.orchestration.temporal.client import start_agent_workflow, store_job_llm_config, store_byok_key

        job.routing = {
            **(job.routing or {}),
            "runtime": "temporal_static",
            "runtime_version": 1,
        }
        payload = job.model_dump()
        payload["config"] = {
            "node_timeout_seconds": settings.AGENT_NODE_TIMEOUT_SECONDS,
            "node_max_retries": settings.AGENT_NODE_MAX_RETRIES,
            "node_concurrency": settings.AGENT_NODE_CONCURRENCY,
        }
        await self.store.create_job(job)
        if llm_config:
            await store_job_llm_config(job.job_id, llm_config)
        elif llm_api_key:
            await store_byok_key(job.job_id, llm_api_key)
        await start_agent_workflow(payload, job.job_id)

    async def submit_manifest(self, job: Job, llm_api_key: str | None, llm_config: dict | None = None) -> None:
        """Persist a compact job reference and start the rolling manifest workflow."""
        from app.agents.orchestration.temporal.client import start_manifest_workflow, store_job_llm_config, store_byok_key

        job.routing = {
            **(job.routing or {}),
            "runtime": "manifest_temporal",
            "runtime_version": 1,
        }
        await self.store.create_job(job)
        if llm_config:
            await store_job_llm_config(job.job_id, llm_config)
        elif llm_api_key:
            await store_byok_key(job.job_id, llm_api_key)
        await start_manifest_workflow(
            {
                "job_id": job.job_id,
                "heartbeat_seconds": settings.TEMPORAL_ACTIVITY_HEARTBEAT_SECONDS,
                "batch_timeout_seconds": max(
                    300,
                    int(settings.AGENT_NODE_TIMEOUT_SECONDS)
                    * max(2, int((job.routing.get("manifest") or {}).get("batch_size") or 1)),
                ),
                "continue_after_batches": settings.TEMPORAL_MANIFEST_CONTINUE_AS_NEW_BATCHES,
            },
            job.job_id,
        )

    async def _signal_manifest(self, job_id: str, signal: str, arg=None) -> None:
        from app.agents.orchestration.temporal.client import signal_manifest_workflow

        await signal_manifest_workflow(job_id, signal, arg)

    async def cancel_manifest(self, job_id: str, keep_completed: bool = True) -> None:
        await self._signal_manifest(job_id, "cancel_request", keep_completed)

    async def pause_manifest(self, job_id: str) -> None:
        await self._signal_manifest(job_id, "pause")

    async def resume_manifest(self, job_id: str) -> None:
        await self._signal_manifest(job_id, "resume")

    @staticmethod
    def can_run_manifest(job: Job) -> bool:
        manifest = (job.routing or {}).get("manifest")
        if not isinstance(manifest, dict):
            return False
        for item in list(manifest.get("items") or []):
            route = str(item.get("route") or item.get("estimated_type") or "")
            if route not in {"direct_llm", "rag"} or list(item.get("subtasks") or []):
                return False
        return True

    @staticmethod
    def can_run_static(job: Job) -> bool:
        """Return whether a job preserves all semantics in the static worker.

        Dynamic replanning, ReAct, approvals, and writes retain the legacy
        runtime until their Temporal equivalents are independently durable.
        """
        routing = job.routing or {}
        if routing.get("manifest") or routing.get("logical_plan"):
            return False
        nodes = list(job.nodes or [])
        if not nodes or len(nodes) > settings.AGENT_PLAN_MAX_NODES:
            return False
        allowed_agents = {"direct_llm", "retrieval", "web_research"}
        for node in nodes:
            if node.agent not in allowed_agents or node.approval:
                return False
            if any(str(getattr(claim, "mode", "read")).lower() == "write" for claim in node.resource_claims):
                return False
        return True

    @staticmethod
    def is_manifest_job(job: Job | None) -> bool:
        return bool(job and str((job.routing or {}).get("runtime") or "") == "manifest_temporal")

    @staticmethod
    def is_static_job(job: Job | None) -> bool:
        return bool(job and str((job.routing or {}).get("runtime") or "") == "temporal_static")
