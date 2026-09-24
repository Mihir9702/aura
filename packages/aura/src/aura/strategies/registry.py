"""The five settled Pods. A registry entry is not an implemented Pod."""

from dataclasses import dataclass

from aura.strategies.lifecycle import Status


@dataclass(frozen=True)
class Pod:
    id: str
    name: str
    description: str
    status: Status = Status.DEVELOPMENT
    implementation: str = "UNIMPLEMENTED"


PODS = (
    Pod("momentum", "Momentum", "Persistence in price and relative strength."),
    Pod("breakout", "Breakout", "Expansion beyond a defined price range."),
    Pod("event-catalyst", "Event / Catalyst", "Price discovery around material events."),
    Pod("mean-reversion", "Mean Reversion", "Dislocations from a defined reference."),
    Pod("swing-trend", "Swing Trend", "Multi-session trends with explicit invalidation."),
)
