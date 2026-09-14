import pytest

from app.agents.orchestration.models import Job, JobStatus, TaskNode, TaskStatus
from app.agents.orchestration.state_machine import (
    InvalidStateTransition,
    can_transition,
    classify_error,
    is_terminal,
    may_retry,
    transition,
)
from app.agents.orchestration.state_machine.errors import OrchestrationError


def test_job_transition_contract_allows_controls_and_blocks_terminal_mutation():
    assert can_transition(JobStatus.RUNNING, JobStatus.PAUSED)
    assert can_transition(JobStatus.PAUSED, JobStatus.RUNNING)
    assert not can_transition(JobStatus.COMPLETED, JobStatus.RUNNING)
    assert is_terminal(JobStatus.FAILED)

    job = Job(job_id="j1", user_id="u1", request="x", status=JobStatus.RUNNING)
    transition(job, JobStatus.PAUSED)
    assert job.status == JobStatus.PAUSED
    with pytest.raises(InvalidStateTransition):
        transition(job, JobStatus.COMPLETED)


def test_error_classification_and_retry_policy_are_stable():
    class RetryableTimeout(OrchestrationError):
        code = "WORKER_TIMEOUT"
        category = "timeout"
        retryable = True
        replannable = True
        user_message = "节点超时"

    info = classify_error(RetryableTimeout("worker took too long"))
    assert info.code == "WORKER_TIMEOUT"
    assert info.retryable and info.replannable
    node = TaskNode(id="n1", agent="worker", status=TaskStatus.FAILED)
    assert may_retry(node, info)
    node.retries = node.max_retries
    assert not may_retry(node, info)

