"""Strategies domain: the Pod registry and lifecycle.

`from aura.strategies import PODS, Status, validate_transition` keeps working from when this
was a single module.
"""

from aura.strategies.lifecycle import Status, validate_transition
from aura.strategies.registry import PODS, Pod

__all__ = ["PODS", "Pod", "Status", "validate_transition"]
