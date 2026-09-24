"""Strategy Pods, horizons, scopes, qualification and activation records (slice S04).

Seeds the five settled Pods at placeholder version 0 (UNIMPLEMENTED), the development horizon
DAILY_MULTI_SESSION_DEV marked UNAPPROVED (OD-03) and one DEVELOPMENT scope per Pod at
version 1, eligibility generation 1. Seeds are frozen here; tests/conftest.py re-seeds the
same rows from aura.strategies.registry, and a unit test compares the two.

Invariants: a scope's identity is immutable, it is never deleted, its version and eligibility
generation never decrease, and any change of state, qualification or activation advances
both. Composite foreign keys bind a qualification to one scope's Pod version and horizon
version, and an activation to that qualification and scope, so an approval cannot be reused
by another version or horizon. Qualification records are append-only. Two partial unique
indexes allow one STRATEGY_STATUS_CHANGED event per scope version and per command id.

The integrator re-chains down_revision at merge.
"""

import sqlalchemy as sa
from alembic import op

revision = "s04_strategy_scopes"
down_revision = "0002"

POD_VERSION = 0
PODS = (
    ("momentum", "MOMENTUM", "Momentum", "Persistence in price and relative strength."),
    ("breakout", "BREAKOUT", "Breakout", "Expansion beyond a defined price range."),
    (
        "event-catalyst",
        "EVENT_CATALYST",
        "Event / Catalyst",
        "Price discovery around material events.",
    ),
    ("mean-reversion", "MEAN_REVERSION", "Mean Reversion", "Dislocations from a defined reference."),
    (
        "swing-trend",
        "SWING_TREND",
        "Swing Trend",
        "Multi-session trends with explicit invalidation.",
    ),
)
HORIZON_ID = "DAILY_MULTI_SESSION_DEV"
HORIZON_VERSION = 1
HORIZON_APPROVAL = "UNAPPROVED"
HORIZON_DECISION = "OD-03"
HORIZON_DESCRIPTION = (
    "Development label only. Horizon definitions, sessions and cadence remain open (OD-03)."
)
SCOPES = tuple(f"{pod[0]}:v{POD_VERSION}:{HORIZON_ID}:v{HORIZON_VERSION}" for pod in PODS)
SEED_STATE = "DEVELOPMENT"
SEED_ACTOR = "migration:s04_strategy_scopes"
SEED_REASON = "Registered at v0: methodology unimplemented; horizon unapproved (OD-03)"

SCHEMA = """
CREATE TABLE strategy_pods (id varchar NOT NULL, version integer NOT NULL CHECK (version >= 0),
 name varchar NOT NULL
  CHECK (name IN ('MOMENTUM','BREAKOUT','EVENT_CATALYST','MEAN_REVERSION','SWING_TREND')),
 display_name varchar NOT NULL, description varchar NOT NULL,
 implementation_status varchar NOT NULL
  CHECK (implementation_status IN ('UNIMPLEMENTED','IMPLEMENTED','VERIFIED')),
 PRIMARY KEY (id, version));
CREATE TABLE strategy_horizons (id varchar NOT NULL, version integer NOT NULL CHECK (version >= 1),
 approval varchar NOT NULL CHECK (approval IN ('UNAPPROVED','APPROVED')),
 decision_ref varchar NOT NULL, description varchar NOT NULL, PRIMARY KEY (id, version));
CREATE TABLE strategy_scopes (id varchar PRIMARY KEY,
 pod_id varchar NOT NULL, pod_version integer NOT NULL,
 horizon_id varchar NOT NULL, horizon_version integer NOT NULL, universe_ref varchar,
 state varchar NOT NULL CHECK (state IN
  ('DEVELOPMENT','BACKTESTING','PAPER_SHADOW','QUALIFIED','ACTIVE_PAPER','SUSPENDED')),
 previous_state varchar CHECK (previous_state IN
  ('DEVELOPMENT','BACKTESTING','PAPER_SHADOW','QUALIFIED','ACTIVE_PAPER','SUSPENDED')),
 version integer NOT NULL CHECK (version >= 1),
 eligibility_generation bigint NOT NULL CHECK (eligibility_generation >= 1),
 qualification_id varchar, activation_id varchar,
 reason varchar NOT NULL, actor varchar NOT NULL, changed_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY (pod_id, pod_version) REFERENCES strategy_pods (id, version),
 FOREIGN KEY (horizon_id, horizon_version) REFERENCES strategy_horizons (id, version),
 UNIQUE NULLS NOT DISTINCT (pod_id, pod_version, horizon_id, horizon_version, universe_ref),
 UNIQUE (id, pod_id, pod_version, horizon_id, horizon_version),
 CONSTRAINT strategy_scope_records CHECK (CASE state
  WHEN 'QUALIFIED' THEN qualification_id IS NOT NULL AND activation_id IS NULL
  WHEN 'ACTIVE_PAPER' THEN qualification_id IS NOT NULL AND activation_id IS NOT NULL
  WHEN 'SUSPENDED' THEN activation_id IS NULL OR qualification_id IS NOT NULL
  ELSE qualification_id IS NULL AND activation_id IS NULL END));
CREATE TABLE strategy_qualifications (id varchar PRIMARY KEY, scope_id varchar NOT NULL,
 pod_id varchar NOT NULL, pod_version integer NOT NULL,
 horizon_id varchar NOT NULL, horizon_version integer NOT NULL,
 policy_ref varchar NOT NULL, evidence_refs jsonb NOT NULL, limitations jsonb NOT NULL,
 approved_by varchar NOT NULL, approved_at timestamptz NOT NULL, valid_until timestamptz,
 UNIQUE (id, scope_id),
 FOREIGN KEY (scope_id, pod_id, pod_version, horizon_id, horizon_version)
  REFERENCES strategy_scopes (id, pod_id, pod_version, horizon_id, horizon_version));
CREATE TABLE strategy_activations (id varchar PRIMARY KEY,
 qualification_id varchar NOT NULL, scope_id varchar NOT NULL,
 portfolio_id varchar NOT NULL REFERENCES portfolios (id),
 generation bigint NOT NULL CHECK (generation >= 1),
 state varchar NOT NULL CHECK (state IN ('ENABLED','DISABLED')),
 approved_by varchar NOT NULL, approved_at timestamptz NOT NULL,
 UNIQUE (id, scope_id),
 FOREIGN KEY (qualification_id, scope_id) REFERENCES strategy_qualifications (id, scope_id));
ALTER TABLE strategy_scopes
 ADD CONSTRAINT strategy_scope_qualification FOREIGN KEY (qualification_id, id)
  REFERENCES strategy_qualifications (id, scope_id),
 ADD CONSTRAINT strategy_scope_activation FOREIGN KEY (activation_id, id)
  REFERENCES strategy_activations (id, scope_id);
CREATE TRIGGER strategy_qualifications_immutable BEFORE UPDATE OR DELETE
 ON strategy_qualifications FOR EACH ROW EXECUTE FUNCTION aura_immutable();
CREATE FUNCTION aura_strategy_scope_guard() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Strategy scopes are never deleted'; END IF;
  IF ROW(NEW.id, NEW.pod_id, NEW.pod_version, NEW.horizon_id, NEW.horizon_version,
         NEW.universe_ref)
     IS DISTINCT FROM ROW(OLD.id, OLD.pod_id, OLD.pod_version, OLD.horizon_id,
         OLD.horizon_version, OLD.universe_ref) THEN
   RAISE EXCEPTION 'Strategy scope identity is immutable';
  END IF;
  IF NEW.version < OLD.version OR NEW.eligibility_generation < OLD.eligibility_generation THEN
   RAISE EXCEPTION 'Strategy scope version and eligibility generation never decrease';
  END IF;
  IF ROW(NEW.state, NEW.qualification_id, NEW.activation_id)
     IS DISTINCT FROM ROW(OLD.state, OLD.qualification_id, OLD.activation_id)
     AND (NEW.version <= OLD.version
          OR NEW.eligibility_generation <= OLD.eligibility_generation) THEN
   RAISE EXCEPTION 'Eligibility changes must advance the scope version and generation';
  END IF;
  RETURN NEW;
 END $$;
CREATE TRIGGER strategy_scopes_guard BEFORE UPDATE OR DELETE ON strategy_scopes
 FOR EACH ROW EXECUTE FUNCTION aura_strategy_scope_guard();
CREATE UNIQUE INDEX outbox_strategy_status_version ON outbox (aggregate_id, aggregate_version)
 WHERE event_type = 'STRATEGY_STATUS_CHANGED';
CREATE UNIQUE INDEX outbox_strategy_status_command ON outbox (correlation_id)
 WHERE event_type = 'STRATEGY_STATUS_CHANGED';
"""


def upgrade():
    op.execute(SCHEMA)
    horizons = sa.table(
        "strategy_horizons",
        *(sa.column(name, sa.String) for name in ("id", "approval", "decision_ref")),
        sa.column("version", sa.Integer),
        sa.column("description", sa.String),
    )
    op.bulk_insert(
        horizons,
        [
            {
                "id": HORIZON_ID,
                "version": HORIZON_VERSION,
                "approval": HORIZON_APPROVAL,
                "decision_ref": HORIZON_DECISION,
                "description": HORIZON_DESCRIPTION,
            }
        ],
    )
    pods = sa.table(
        "strategy_pods",
        *(sa.column(name, sa.String) for name in ("id", "name", "display_name", "description")),
        sa.column("version", sa.Integer),
        sa.column("implementation_status", sa.String),
    )
    op.bulk_insert(
        pods,
        [
            {
                "id": pod_id,
                "version": POD_VERSION,
                "name": name,
                "display_name": display_name,
                "description": description,
                "implementation_status": "UNIMPLEMENTED",
            }
            for pod_id, name, display_name, description in PODS
        ],
    )
    scopes = sa.table(
        "strategy_scopes",
        *(
            sa.column(name, sa.String)
            for name in ("id", "pod_id", "horizon_id", "state", "reason", "actor")
        ),
        *(
            sa.column(name, sa.Integer)
            for name in ("pod_version", "horizon_version", "version", "eligibility_generation")
        ),
    )
    op.bulk_insert(
        scopes,
        [
            {
                "id": scope,
                "pod_id": pod[0],
                "pod_version": POD_VERSION,
                "horizon_id": HORIZON_ID,
                "horizon_version": HORIZON_VERSION,
                "state": SEED_STATE,
                "version": 1,
                "eligibility_generation": 1,
                "reason": SEED_REASON,
                "actor": SEED_ACTOR,
            }
            for scope, pod in zip(SCOPES, PODS, strict=True)
        ],
    )


def downgrade():
    # Status events stay in the append-only outbox; dropping their scopes would orphan them.
    op.execute("""
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM outbox WHERE event_type = 'STRATEGY_STATUS_CHANGED') THEN
  RAISE EXCEPTION 'Strategy status history exists; restore a verified backup instead';
 END IF;
END $$;
DROP INDEX outbox_strategy_status_command;
DROP INDEX outbox_strategy_status_version;
DROP TABLE strategy_scopes, strategy_activations, strategy_qualifications, strategy_horizons,
 strategy_pods;
DROP FUNCTION aura_strategy_scope_guard();
""")
