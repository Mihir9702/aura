"""The five settled Pods and the registry rows migration s04_strategy_scopes seeds for them.

A registry entry is not an implemented Pod. Version 0 is a placeholder with no reviewed
methodology (implementation status UNIMPLEMENTED); reviewed methodology versions start at 1,
matching the positive-integer Version of the contracts. DAILY_MULTI_SESSION_DEV is a
development horizon label, not an approved horizon definition: horizons remain OD-03.
"""

from dataclasses import dataclass
from enum import StrEnum

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


class PodName(StrEnum):
    """Contract names of the five settled Pods (StrategyPod.name)."""

    MOMENTUM = "MOMENTUM"
    BREAKOUT = "BREAKOUT"
    EVENT_CATALYST = "EVENT_CATALYST"
    MEAN_REVERSION = "MEAN_REVERSION"
    SWING_TREND = "SWING_TREND"


def pod_name(pod_id: str) -> PodName:
    return PodName(pod_id.upper().replace("-", "_"))


PLACEHOLDER_POD_VERSION = 0
DEV_HORIZON_ID = "DAILY_MULTI_SESSION_DEV"
DEV_HORIZON_VERSION = 1
DEV_HORIZON_DECISION = "OD-03"
DEV_HORIZON_DESCRIPTION = (
    "Development label only. Horizon definitions, sessions and cadence remain open (OD-03)."
)
SEED_ACTOR = "migration:s04_strategy_scopes"
SEED_REASON = "Registered at v0: methodology unimplemented; horizon unapproved (OD-03)"


def scope_id(pod_id: str, pod_version: int, horizon_id: str, horizon_version: int) -> str:
    """Stable id of the scope binding a Pod version to a horizon version."""
    return f"{pod_id}:v{pod_version}:{horizon_id}:v{horizon_version}"


SEEDED_SCOPE_IDS = tuple(
    scope_id(pod.id, PLACEHOLDER_POD_VERSION, DEV_HORIZON_ID, DEV_HORIZON_VERSION) for pod in PODS
)
