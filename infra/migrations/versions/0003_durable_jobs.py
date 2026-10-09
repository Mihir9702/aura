"""Durable jobs: leased, fenced, bounded-retry work items with visible dead letters.

Identity columns are immutable and the fencing token and version only move forward, so a
redriven dead letter keeps its identity and no superseded lease can be reused.
"""
from alembic import op

revision = "0003_durable_jobs"
down_revision = "0002"


def upgrade():
    op.execute("""
CREATE TABLE jobs (id varchar PRIMARY KEY, kind varchar NOT NULL,
 job_class varchar NOT NULL
  CHECK (job_class IN ('RECONCILIATION','MONITORING','INGEST','ANALYSIS','RESEARCH')),
 scope varchar NOT NULL, occurrence_key varchar NOT NULL UNIQUE,
 correlation_id varchar NOT NULL, payload jsonb NOT NULL, input_version integer,
 priority integer NOT NULL, scheduled_at timestamptz NOT NULL,
 available_at timestamptz NOT NULL, deadline timestamptz CHECK (deadline > scheduled_at),
 state varchar NOT NULL CHECK (state IN ('QUEUED','RUNNING','SUCCEEDED','DEAD','EXPIRED')),
 version integer NOT NULL CHECK (version >= 1),
 attempts integer NOT NULL CHECK (attempts >= 0),
 max_attempts integer NOT NULL CHECK (max_attempts >= 1),
 redrives integer NOT NULL CHECK (redrives >= 0),
 fencing_token bigint NOT NULL CHECK (fencing_token >= 0),
 lease_owner varchar, lease_expires_at timestamptz,
 result jsonb, terminal_reason varchar, last_error varchar,
 created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL, finished_at timestamptz,
 CHECK (attempts <= max_attempts),
 CHECK ((state = 'RUNNING') = (lease_owner IS NOT NULL)),
 CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL)),
 CHECK ((state IN ('SUCCEEDED','DEAD','EXPIRED')) = (finished_at IS NOT NULL)),
 CHECK ((state IN ('DEAD','EXPIRED')) = (terminal_reason IS NOT NULL)));
CREATE INDEX jobs_pending ON jobs (available_at) WHERE state IN ('QUEUED','RUNNING');
CREATE INDEX jobs_dead_letters ON jobs (finished_at) WHERE state = 'DEAD';
CREATE INDEX jobs_created ON jobs (created_at);
CREATE FUNCTION aura_job_update() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN
  IF (NEW.id, NEW.kind, NEW.job_class, NEW.scope, NEW.occurrence_key, NEW.correlation_id,
      NEW.payload, NEW.input_version, NEW.scheduled_at, NEW.deadline, NEW.created_at)
     IS DISTINCT FROM
     (OLD.id, OLD.kind, OLD.job_class, OLD.scope, OLD.occurrence_key, OLD.correlation_id,
      OLD.payload, OLD.input_version, OLD.scheduled_at, OLD.deadline, OLD.created_at) THEN
   RAISE EXCEPTION 'Job identity is immutable';
  END IF;
  IF NEW.fencing_token < OLD.fencing_token OR NEW.version <= OLD.version THEN
   RAISE EXCEPTION 'Job fencing token and version only move forward';
  END IF;
  RETURN NEW;
 END $$;
CREATE TRIGGER jobs_update BEFORE UPDATE ON jobs FOR EACH ROW EXECUTE FUNCTION aura_job_update();
""")


def downgrade():
    raise RuntimeError("Forward-only jobs migration; restore a verified backup instead")
