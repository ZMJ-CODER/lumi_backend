import asyncio

from app.agents.orchestration.approval_service import ApprovalService
from app.agents.orchestration.models import Job, JobStatus, TaskNode, TaskStatus
from app.agents.orchestration.state import InMemoryStateStore


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
