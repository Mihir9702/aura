"""Execution became a package in S05; earlier import paths must keep working."""

import subprocess
import sys
from typing import get_args

import aura.execution as execution
import pytest
from aura import ledger
from aura.contracts import QuarantineReason
from aura.execution import Reason
from aura.ledger import FixtureOrder, apply_fixture_fill, reserve_fixture


def test_moved_fixture_names_still_import_from_the_ledger():
    assert FixtureOrder is execution.FixtureOrder
    assert reserve_fixture is execution.reserve_fixture
    assert apply_fixture_fill is execution.apply_fixture_fill
    with pytest.raises(AttributeError):
        getattr(ledger, "not_a_ledger_name")  # noqa: B009


def test_execution_package_keeps_its_earlier_interface():
    for name in ("PaperAdapter", "admit_fixture", "record_submission", "cancel_confirmed_fixture"):
        assert callable(getattr(execution, name))


@pytest.mark.parametrize(
    "statement",
    ["import aura.execution", "from aura.ledger import FixtureOrder, reserve_fixture"],
)
def test_either_module_can_be_imported_first(statement):
    # A fresh interpreter proves there is no import cycle in either order.
    subprocess.run([sys.executable, "-c", statement], check=True)


def test_quarantine_contract_lists_every_reason():
    assert set(get_args(QuarantineReason)) == {reason.value for reason in Reason}
