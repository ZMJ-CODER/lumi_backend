"""事件契约：流式事件、生命周期状态机、审批状态。"""

from __future__ import annotations

from lumi_contracts.events.approval import (
    ApprovalDecision,
    ApprovalScope,
    ApprovalState,
    approval_fingerprint,
)
from lumi_contracts.events.lifecycle import (
    TERMINAL_STATES,
    RunState,
    assert_transition,
    can_transition,
)
from lumi_contracts.events.stream import (
    KNOWN_EVENT_TYPES,
    EventSequencer,
    StreamEvent,
    StreamEventType,
)

__all__ = [
    "ApprovalDecision",
    "ApprovalScope",
    "ApprovalState",
    "EventSequencer",
    "KNOWN_EVENT_TYPES",
    "RunState",
    "StreamEvent",
    "StreamEventType",
    "TERMINAL_STATES",
    "approval_fingerprint",
    "assert_transition",
    "can_transition",
]
