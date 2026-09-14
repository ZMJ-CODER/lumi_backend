import pytest
from pydantic import ValidationError

from lumi_orch import IdempotencySpec, NodeExecutionSpec, NodeSpec


def test_write_once_requires_keyed_idempotency():
    with pytest.raises(ValidationError):
        NodeExecutionSpec(
            side_effect="write_once",
            idempotency=IdempotencySpec(type="explicit_key"),
        )


def test_critical_and_isolated_are_mutually_exclusive():
    with pytest.raises(ValidationError):
        NodeExecutionSpec(critical=True, failure_isolation=True)


def test_node_execution_spec_is_backward_compatible():
    node = NodeSpec(id="n1", agent="reader")
    assert node.execution.side_effect == "pure_read"
    assert node.execution.failure_isolation is False

