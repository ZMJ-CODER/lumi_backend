import asyncio

import pytest

from lumi_orch.admission import AdmissionBackpressureError, AdmissionLimits, JobAdmission


def test_local_admission_enforces_submission_and_user_limits():
    limits = AdmissionLimits(lease_seconds=60, max_inflight=1, max_active_jobs=2, max_active_jobs_per_user=1)
    admission = JobAdmission(limits_provider=lambda: limits)

    async def scenario():
        await admission.reserve("first")
        with pytest.raises(AdmissionBackpressureError):
            await admission.reserve("second")
        await admission.promote("first", "job-1", "user-1")
        await admission.reserve("second")
        with pytest.raises(AdmissionBackpressureError):
            await admission.promote("second", "job-2", "user-1")

    asyncio.run(scenario())


def test_local_lease_renews_only_existing_jobs():
    admission = JobAdmission()

    async def scenario():
        await admission.reserve("token")
        await admission.promote("token", "job-1", "user-1")
        assert await admission.renew("job-1", "user-1") is True
        assert await admission.renew("missing", "user-1") is False

    asyncio.run(scenario())


def test_local_admission_can_reactivate_a_suspended_job_without_bypassing_limits():
    limits = AdmissionLimits(lease_seconds=60, max_inflight=2, max_active_jobs=1, max_active_jobs_per_user=1)
    admission = JobAdmission(limits_provider=lambda: limits)

    async def scenario():
        await admission.reserve("first")
        await admission.promote("first", "job-1", "user-1")
        await admission.release(job_id="job-1", user_id="user-1")
        await admission.reserve("second")
        await admission.promote("second", "job-2", "user-2")
        with pytest.raises(AdmissionBackpressureError):
            await admission.activate("job-1", "user-1")
        await admission.release(job_id="job-2", user_id="user-2")
        await admission.activate("job-1", "user-1")
        assert await admission.renew("job-1", "user-1") is True

    asyncio.run(scenario())
