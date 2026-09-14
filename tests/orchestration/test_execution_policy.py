from app.agents.orchestration.policy.execution_defaults import load_execution_defaults, resolve_node_execution_spec
from lumi_orch import NodeExecutionSpec
from app.agents.orchestration.runtime.job_contract import freeze_job_spec
from app.agents.orchestration.models import Job, TaskNode


def test_execution_defaults_are_bounded_and_versioned():
    document = load_execution_defaults()
    assert document.version == 1
    assert document.defaults["io_bound"].timeout_seconds == 60
    assert document.concurrency["agent"] == 2


def test_resolution_returns_reproducible_policy_snapshot():
    spec, snapshot = resolve_node_execution_spec(NodeExecutionSpec(resource_class="cpu_bound"))
    assert spec.timeout_seconds == 120
    assert spec.retry.max_attempts == 2
    assert snapshot["version"] == 1
    assert len(snapshot["sha256"]) == 64


def test_freeze_job_persists_node_policy_snapshot():
    job = Job(job_id="j1", user_id="u1", request="读一份文件", nodes=[TaskNode(id="n1", agent="reader")])
    spec = freeze_job_spec(job)
    assert spec.nodes[0].execution.timeout_seconds == 60
    assert job.routing["execution_spec"]["policy_snapshots"]["n1"]["version"] == 1


def test_freeze_job_derives_long_generation_timeout_from_node_budget():
    job = Job(
        job_id="long-1",
        user_id="u1",
        request="写一份不少于 3000 字的完整报告",
        nodes=[
            TaskNode(
                id="report",
                agent="direct_llm",
                params={
                    "instruction": "写一份不少于 3000 字的完整报告",
                    "max_tokens": 3800,
                    "timeout_hint": "long_generation",
                },
            )
        ],
    )
    spec = freeze_job_spec(job)
    assert spec.nodes[0].execution.timeout_seconds >= 120
    assert spec.nodes[0].execution.timeout_seconds > 60
