"""Execution: fixture Order Manager primitives, paper adapter protocol and durable receipts.

SHADOW fixtures only. No vendor, live adapter, credential or active order route exists, and
Observe stays the only mode. Receipts commit before the Ledger applies them, each fill is
applied at most once, and invalid receipts are quarantined for an audited owner redrive
instead of being raised and rolled back.
"""

from aura.execution.adapter import FIXTURE_ACCOUNT_ID, FIXTURE_ADAPTER_ID, PaperAdapter
from aura.execution.fills import (
    RECEIPT_CONSUMER,
    FillOutcome,
    FixtureFill,
    Recorded,
    RedriveOutcome,
    apply_fixture_fill,
    apply_receipt,
    apply_received_fills,
    handle_fill_received,
    receive_fixture_fill,
    record_fill,
    redrive_quarantine,
)
from aura.execution.orders import (
    FixtureOrder,
    admit_fixture,
    cancel_confirmed_fixture,
    record_submission,
    reserve_fixture,
)
from aura.execution.receipts import (
    ExecutionReceipt,
    QuarantineEntry,
    Reason,
    blocking_quarantine,
    open_quarantine,
)

__all__ = [
    "FIXTURE_ACCOUNT_ID",
    "FIXTURE_ADAPTER_ID",
    "RECEIPT_CONSUMER",
    "ExecutionReceipt",
    "FillOutcome",
    "FixtureFill",
    "FixtureOrder",
    "PaperAdapter",
    "QuarantineEntry",
    "Reason",
    "Recorded",
    "RedriveOutcome",
    "admit_fixture",
    "apply_fixture_fill",
    "apply_receipt",
    "apply_received_fills",
    "blocking_quarantine",
    "cancel_confirmed_fixture",
    "handle_fill_received",
    "open_quarantine",
    "receive_fixture_fill",
    "record_fill",
    "record_submission",
    "redrive_quarantine",
    "reserve_fixture",
]
