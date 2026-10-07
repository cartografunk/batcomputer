-- Apply after 001_initial.sql. Additive migration; no existing data is removed.
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS pending_clarification text;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS recovery_count integer NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS validation_status varchar(24) NOT NULL DEFAULT 'not_executed';
CREATE TABLE IF NOT EXISTS validations (id varchar(36) PRIMARY KEY, run_id varchar(36) NOT NULL REFERENCES runs(id), attempt integer NOT NULL, status varchar(24) NOT NULL, content jsonb NOT NULL);
CREATE INDEX IF NOT EXISTS ix_validations_run_id ON validations(run_id);
ALTER TABLE validations ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON validations FROM anon, authenticated;
