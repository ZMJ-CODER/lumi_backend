"""纯编排状态机契约。

The runtime adapters still own persistence and side effects.  This package
owns only the rules for deciding whether a Job transition is valid.
"""

from app.agents.orchestration.state_machine.errors import (
    ErrorCategory,
    ErrorInfo,
    OrchestrationError,
    classify_error,
)
from app.agents.orchestration.state_machine.policies import (
    is_terminal,
    may_escalate,
    may_retry,
    may_replan,
)
from app.agents.orchestration.state_machine.transitions import (
    InvalidStateTransition,
    can_transition,
    transition,
)

__all__ = [
    "ErrorCategory",
    "ErrorInfo",
    "InvalidStateTransition",
    "OrchestrationError",
    "classify_error",
    "is_terminal",
    "may_escalate",
    "may_retry",
    "may_replan",
    "can_transition",
    "transition",
]
