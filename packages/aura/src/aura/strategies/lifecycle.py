"""Strategy lifecycle states and the transition rule of the target progression."""

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
