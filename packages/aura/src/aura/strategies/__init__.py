"""Strategies domain: the Pod registry, lifecycle, persisted scopes and entry eligibility.

Persistence (aura.strategies.models) is private to this package. Other modules use the
functions exported here, and `from aura.strategies import PODS, Status, validate_transition`
keeps working from when this was a single module.
"""

from aura.strategies.eligibility import (
    EligibilityDecision,
    ScopeEligibility,
    check_entry_eligibility,
    entry_blockers,
    lock_scopes,
)
from aura.strategies.lifecycle import Status, scope_transition_refusal, validate_transition
from aura.strategies.registry import PODS, SEEDED_SCOPE_IDS, Pod, PodName, scope_id
from aura.strategies.scopes import (
    STATUS_CHANGED,
    StrategyRefusal,
    UnknownScope,
    get_scope,
    list_scopes,
    resume,
    scope_history,
    suspend,
    transition,
)

__all__ = [
    "PODS",
    "SEEDED_SCOPE_IDS",
    "STATUS_CHANGED",
    "EligibilityDecision",
    "Pod",
    "PodName",
    "ScopeEligibility",
    "Status",
    "StrategyRefusal",
    "UnknownScope",
    "check_entry_eligibility",
    "entry_blockers",
    "get_scope",
    "list_scopes",
    "lock_scopes",
    "resume",
    "scope_history",
    "scope_id",
    "scope_transition_refusal",
    "suspend",
    "transition",
    "validate_transition",
]
