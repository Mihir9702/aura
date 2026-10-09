"""Durable execution receipts and quarantine for SHADOW fixture fills (S05).

A receipt is unique per adapter, account, execution ID and revision. Its facts are immutable;
only its status advances, and an applied receipt is final with exactly one journal. A
quarantine entry keeps its receipt, the delivery that caused it and its creation time; its
diagnosis may change on redrive until it is resolved, which is final. Neither table allows
DELETE. New tables only; existing journals, postings and outbox are untouched.
"""
from alembic import op

revision = "s05_execution_receipts"
down_revision = "s04_strategy_scopes"


def upgrade():
    op.execute("""
CREATE TABLE execution_receipts (id varchar PRIMARY KEY,
 adapter_id varchar NOT NULL CHECK (adapter_id <> ''),
 adapter_account_id varchar NOT NULL CHECK (adapter_account_id <> ''),
 execution_id varchar NOT NULL CHECK (execution_id <> ''),
 revision integer NOT NULL CHECK (revision >= 1),
 order_id varchar NOT NULL CHECK (order_id <> ''),
 quantity numeric(28,8) NOT NULL, price numeric(28,8) NOT NULL, fee numeric(28,8) NOT NULL,
 policy varchar NOT NULL, received_at timestamptz NOT NULL DEFAULT now(),
 status varchar NOT NULL DEFAULT 'RECEIVED' CHECK (status IN ('RECEIVED','APPLIED','QUARANTINED')),
 journal_id varchar UNIQUE REFERENCES journals,
 CONSTRAINT execution_receipts_identity UNIQUE (adapter_id, adapter_account_id, execution_id, revision),
 CONSTRAINT execution_receipts_applied CHECK ((status = 'APPLIED') = (journal_id IS NOT NULL)));
CREATE TABLE execution_quarantine (id varchar PRIMARY KEY,
 receipt_id varchar NOT NULL REFERENCES execution_receipts,
 portfolio_id varchar REFERENCES portfolios,
 reason varchar NOT NULL CHECK (reason IN ('CONFLICTING_DUPLICATE','UNSUPPORTED_REVISION',
  'UNKNOWN_ACCOUNT','ORPHAN','OUT_OF_BOUNDS','LEDGER_REJECTED')),
 detail varchar NOT NULL, delivered jsonb NOT NULL,
 status varchar NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','RESOLVED')),
 version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
 created_at timestamptz NOT NULL DEFAULT now(), resolved_at timestamptz,
 CONSTRAINT execution_quarantine_resolution CHECK ((status = 'RESOLVED') = (resolved_at IS NOT NULL)));
CREATE INDEX execution_quarantine_receipt ON execution_quarantine (receipt_id);
CREATE INDEX execution_quarantine_open ON execution_quarantine (portfolio_id) WHERE status = 'OPEN';
CREATE FUNCTION aura_receipt_guard() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Execution receipts are append-only'; END IF;
  IF (NEW.id, NEW.adapter_id, NEW.adapter_account_id, NEW.execution_id, NEW.revision, NEW.order_id,
      NEW.quantity, NEW.price, NEW.fee, NEW.policy, NEW.received_at)
     IS DISTINCT FROM (OLD.id, OLD.adapter_id, OLD.adapter_account_id, OLD.execution_id,
      OLD.revision, OLD.order_id, OLD.quantity, OLD.price, OLD.fee, OLD.policy, OLD.received_at) THEN
   RAISE EXCEPTION 'Execution receipt facts are immutable';
  END IF;
  IF OLD.status = 'APPLIED' THEN RAISE EXCEPTION 'An applied execution receipt is final'; END IF;
  RETURN NEW;
 END $$;
CREATE TRIGGER execution_receipts_guard BEFORE UPDATE OR DELETE ON execution_receipts
 FOR EACH ROW EXECUTE FUNCTION aura_receipt_guard();
CREATE FUNCTION aura_quarantine_guard() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Execution quarantine is append-only'; END IF;
  IF (NEW.id, NEW.receipt_id, NEW.delivered, NEW.created_at)
     IS DISTINCT FROM (OLD.id, OLD.receipt_id, OLD.delivered, OLD.created_at) THEN
   RAISE EXCEPTION 'Quarantine evidence is immutable';
  END IF;
  IF OLD.status = 'RESOLVED' THEN RAISE EXCEPTION 'A resolved quarantine entry is final'; END IF;
  RETURN NEW;
 END $$;
CREATE TRIGGER execution_quarantine_guard BEFORE UPDATE OR DELETE ON execution_quarantine
 FOR EACH ROW EXECUTE FUNCTION aura_quarantine_guard();
""")


def downgrade():
    raise RuntimeError("Forward-only accounting migration")
