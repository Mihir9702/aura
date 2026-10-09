"""Strategy lifecycle states and transition rules.

`validate_transition` checks the target progression of the design. Persisted scope commands
use `scope_transition_refusal`, which is stricter while policy is open: no approved
qualification policy exists (OD-15) and Research registers no experiments yet, so only
suspension and a resume to DEVELOPMENT can be applied.
"""

from enum import StrEnum


class Status(StrEnum):
    DEVELOPMENT = "DEVELOPMENT"
    BACKTESTING = "BACKTESTING"
    PAPER_SHADOW = "PAPER_SHADOW"
    QUALIFIED = "QUALIFIED"
    ACTIVE_PAPER = "ACTIVE_PAPER"
    SUSPENDED = "SUSPENDED"


def validate_transition(
    previous: Status, target: Status, *, evidence: bool, human_approval: bool
) -> None:
    next_state = {
        Status.DEVELOPMENT: Status.BACKTESTING,
        Status.BACKTESTING: Status.PAPER_SHADOW,
        Status.PAPER_SHADOW: Status.QUALIFIED,
        Status.QUALIFIED: Status.ACTIVE_PAPER,
    }
    if target == Status.SUSPENDED:
        return
    if next_state.get(previous) != target:
        raise ValueError(
            "Unsupported lifecycle transition; resume requires reviewed requalification"
        )
    if target in {Status.QUALIFIED, Status.ACTIVE_PAPER} and not (evidence and human_approval):
        raise ValueError("Evidence and human approval are required")


QUALIFICATION_POLICY_MISSING = "QUALIFICATION_POLICY_MISSING"
EVALUATION_EVIDENCE_MISSING = "EVALUATION_EVIDENCE_MISSING"
RESUME_REQUIRES_SUSPENDED = "RESUME_REQUIRES_SUSPENDED"

REFUSALS = {
    QUALIFICATION_POLICY_MISSING: (
        "No approved qualification policy exists (OD-15); QUALIFIED and ACTIVE_PAPER are "
        "unavailable"
    ),
    EVALUATION_EVIDENCE_MISSING: (
        "BACKTESTING and PAPER_SHADOW require registered experiment evidence, which Research "
        "does not provide yet"
    ),
    RESUME_REQUIRES_SUSPENDED: "Only a suspended scope can resume, and it resumes to DEVELOPMENT",
}


def scope_transition_refusal(current: Status, target: Status) -> str | None:
    """Refusal code for a persisted scope transition, or None when it may be applied.

    SUSPENDED is allowed from any state, SUSPENDED included (it records another cause).
    Resume returns a suspended scope to DEVELOPMENT only.
    """
    if target in (Status.QUALIFIED, Status.ACTIVE_PAPER):
        return QUALIFICATION_POLICY_MISSING
    if target in (Status.BACKTESTING, Status.PAPER_SHADOW):
        return EVALUATION_EVIDENCE_MISSING
    if target == Status.SUSPENDED:
        return None
    return None if current == Status.SUSPENDED else RESUME_REQUIRES_SUSPENDED
