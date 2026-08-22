import asyncio

from app.agents.orchestration.admission_lease import AdmissionLeaseMonitor
from app.agents.orchestration.job_error_service import JobErrorService
from app.agents.orchestration.models import Job, JobStatus
from app.agents.orchestration.state import InMemoryStateStore


def test_admission_lease_monitor_stops_and_interrupts_after_lease_loss(monkeypatch):
    async def scenario():
        store = InMemoryStateStore()
        job = Job(job_id="lease-1", user_id="u1", request="x", status=JobStatus.RUNNING)
        await store.create_job(job)
        tasks = {}
        monitor = AdmissionLeaseMonitor(
            store=store,
            tasks=tasks,
            error_service=JobErrorService(store=store),
            interval_seconds=0,
        )

        async def renew(_job_id, _user_id):
            return False

        monkeypatch.setattr(
            "app.agents.orchestration.admission_lease.job_admission.renew",
            renew,
        )
        monitor.start("lease-1", "u1")
        await asyncio.sleep(0.02)
        await monitor.stop("lease-1")
        saved = await store.get_job("lease-1")
        assert saved.status == JobStatus.INTERRUPTED

    asyncio.run(scenario())
