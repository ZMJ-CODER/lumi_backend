import asyncio
import time

from app.agents.orchestration.approval_service import ApprovalService
from app.agents.orchestration.models import Job, JobStatus, TaskNode, TaskStatus
from app.agents.orchestration.state import InMemoryStateStore
from app.agents.skills.executor import tool_call_fingerprint


def test_approval_service_binds_confirmation_and_resumes_node():
    async def scenario():
        store = InMemoryStateStore()
        job = Job(
            job_id="approval-1",
            user_id="u1",
            request="delete",
            status=JobStatus.WAITING_APPROVAL,
            nodes=[TaskNode(
                id="n1",
                agent="worker",
                status=TaskStatus.ESCALATED,
                metadata={
                    "awaiting_approval": True,
                    "approval_tool": "delete_file",
                    "approval_fingerprint": "fp-1",
                },
            )],
        )
        await store.create_job(job)
        result = await ApprovalService(store=store).resolve("approval-1", "n1", True)
        assert result.approved
        assert result.job.status == JobStatus.RUNNING
        assert result.job.nodes[0].status == TaskStatus.PENDING
        assert result.job.nodes[0].metadata["confirmed_tool_calls"] == ["fp-1"]

    asyncio.run(scenario())


def test_approval_fingerprint_changes_when_upstream_content_changes():
    args = {"to": "alice@example.com", "body": "report"}
    assert tool_call_fingerprint("send_email", args, "result-v1") != tool_call_fingerprint(
        "send_email", args, "result-v2"
    )


def test_approval_expiry_fails_waiting_job():
    async def scenario():
        store = InMemoryStateStore()
        job = Job(
            job_id="approval-expired",
            user_id="u1",
            request="delete",
            status=JobStatus.WAITING_APPROVAL,
            nodes=[TaskNode(
                id="n1",
                agent="worker",
                status=TaskStatus.PENDING,
                metadata={
                    "awaiting_approval": True,
                    "approval_expires_at": time.time() - 1,
                },
            )],
        )
        await store.create_job(job)
        expired = await ApprovalService(store=store).expire_if_due(job)
        saved = await store.get_job(job.job_id)
        assert expired is True
        assert saved.status == JobStatus.FAILED
        assert saved.nodes[0].error_code == "APPROVAL_TIMEOUT"

    asyncio.run(scenario())


def test_admission_heartbeat_expires_approval_without_a_client_poll(monkeypatch):
    async def scenario():
        from app.agents.orchestration.admission_lease import AdmissionLeaseMonitor
        from app.agents.orchestration.job_error_service import JobErrorService

        releases = []

        class Admission:
            async def release(self, **kwargs):
                releases.append(kwargs)

            async def renew(self, *_args):
                return True

        monkeypatch.setattr("app.agents.orchestration.admission_lease.job_admission", Admission())
        store = InMemoryStateStore()
        job = Job(
            job_id="approval-heartbeat",
            user_id="u1",
            request="delete",
            status=JobStatus.WAITING_APPROVAL,
            nodes=[TaskNode(
                id="n1",
                agent="worker",
                metadata={
                    "awaiting_approval": True,
                    "approval_expires_at": time.time() - 1,
                },
            )],
        )
        await store.create_job(job)
        monitor = AdmissionLeaseMonitor(
            store=store,
            tasks={},
            error_service=JobErrorService(store=store),
            interval_seconds=0.001,
        )
        monitor.start(job.job_id, job.user_id)
        await asyncio.sleep(0.03)
        saved = await store.get_job(job.job_id)
        assert saved.status == JobStatus.FAILED
        assert releases == [{"job_id": job.job_id, "user_id": job.user_id}]
        await monitor.stop(job.job_id)

    asyncio.run(scenario())
