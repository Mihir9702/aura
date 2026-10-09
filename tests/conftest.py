"""Shared PostgreSQL integration harness.

Integration tests reset only a dedicated loopback database named aura_test or
aura_test_<suffix> (lowercase letters and digits). scripts/test-integration.ps1 selects
it from AURA_TEST_DATABASE_NAME, so worktrees can run concurrently against one cluster
by using different names. The development database never matches the pattern.
"""

import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from aura.ledger import fund
from aura.operations import Gate
from aura.storage import Database
from aura.strategies import registry
from aura.strategies.models import HorizonRow, PodVersionRow, ScopeRow
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

TEST_DATABASE_NAME = re.compile(r"aura_test(_[a-z0-9]+)?")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def seed_strategy_registry(session: Session) -> None:
    """Re-seed migration s04_strategy_scopes' rows: five Pods at v0, the unapproved
    DAILY_MULTI_SESSION_DEV horizon and one DEVELOPMENT scope per Pod."""
    session.add(
        HorizonRow(
            id=registry.DEV_HORIZON_ID,
            version=registry.DEV_HORIZON_VERSION,
            approval="UNAPPROVED",
            decision_ref=registry.DEV_HORIZON_DECISION,
            description=registry.DEV_HORIZON_DESCRIPTION,
        )
    )
    for pod in registry.PODS:
        session.add(
            PodVersionRow(
                id=pod.id,
                version=registry.PLACEHOLDER_POD_VERSION,
                name=registry.pod_name(pod.id),
                display_name=pod.name,
                description=pod.description,
                implementation_status="UNIMPLEMENTED",
            )
        )
    session.flush()
    for pod, scope in zip(registry.PODS, registry.SEEDED_SCOPE_IDS, strict=True):
        session.add(
            ScopeRow(
                id=scope,
                pod_id=pod.id,
                pod_version=registry.PLACEHOLDER_POD_VERSION,
                horizon_id=registry.DEV_HORIZON_ID,
                horizon_version=registry.DEV_HORIZON_VERSION,
                state="DEVELOPMENT",
                version=1,
                eligibility_generation=1,
                reason=registry.SEED_REASON,
                actor=registry.SEED_ACTOR,
                changed_at=datetime.now(UTC),
            )
        )
    session.flush()


def reset_database(database: Database) -> None:
    """Truncate every public table except alembic_version, then re-seed gate and funding.

    Discovering tables from the catalog keeps the reset complete when migrations add
    tables. The seed mirrors migration 0001's global gate row plus the challenge and
    SHADOW fixture funding the integration tests rely on, and migration
    s04_strategy_scopes' strategy registry rows.
    """
    with database.transaction() as session:
        tables = session.scalars(
            text(
                "SELECT quote_ident(schemaname) || '.' || quote_ident(tablename) "
                "FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version' "
                "ORDER BY tablename"
            )
        ).all()
        if tables:
            session.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
        session.add(Gate(id="global", version=1, entry_halt=False, full_kill=False))
        session.flush()
        fund(session, "challenge")
        fund(session, "fixture", environment="SHADOW")
        seed_strategy_registry(session)


@pytest.fixture
def db() -> Iterator[Database]:
    url = os.environ.get("AURA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Dedicated test database required; scripts/test-integration.ps1")
    target = make_url(url)
    if target.host not in LOOPBACK_HOSTS or not TEST_DATABASE_NAME.fullmatch(
        target.database or ""
    ):
        raise RuntimeError("Refusing to reset anything except a loopback aura_test database")
    database = Database(url)
    try:
        reset_database(database)
        yield database
    finally:
        database.engine.dispose()
