-- Apply to Supabase/PostgreSQL before starting the API.
CREATE SCHEMA IF NOT EXISTS batcomputer;
REVOKE ALL ON SCHEMA batcomputer FROM PUBLIC, anon, authenticated;
CREATE TABLE IF NOT EXISTS batcomputer.conversations (id varchar(36) PRIMARY KEY, owner_hash varchar(64) NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS ix_conversations_owner_hash ON batcomputer.conversations(owner_hash);
CREATE TABLE IF NOT EXISTS batcomputer.messages (id varchar(36) PRIMARY KEY, conversation_id varchar(36) NOT NULL REFERENCES batcomputer.conversations(id), role varchar(16) NOT NULL, content text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS ix_messages_conversation_id ON batcomputer.messages(conversation_id);
CREATE TABLE IF NOT EXISTS batcomputer.runs (id varchar(36) PRIMARY KEY, conversation_id varchar(36) NOT NULL REFERENCES batcomputer.conversations(id), message_id varchar(36) NOT NULL REFERENCES batcomputer.messages(id), status varchar(24) NOT NULL DEFAULT 'queued', result text, attempts integer NOT NULL DEFAULT 0, api_retries integer NOT NULL DEFAULT 0, error text, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), heartbeat_at timestamptz, lease_id varchar(36));
CREATE INDEX IF NOT EXISTS ix_runs_conversation_id ON batcomputer.runs(conversation_id);
CREATE INDEX IF NOT EXISTS ix_runs_status ON batcomputer.runs(status);
CREATE TABLE IF NOT EXISTS batcomputer.plans (id varchar(36) PRIMARY KEY, run_id varchar(36) NOT NULL REFERENCES batcomputer.runs(id), content jsonb NOT NULL);
CREATE INDEX IF NOT EXISTS ix_plans_run_id ON batcomputer.plans(run_id);
CREATE TABLE IF NOT EXISTS batcomputer.reviews (id varchar(36) PRIMARY KEY, run_id varchar(36) NOT NULL REFERENCES batcomputer.runs(id), attempt integer NOT NULL, content jsonb NOT NULL);
CREATE INDEX IF NOT EXISTS ix_reviews_run_id ON batcomputer.reviews(run_id);
CREATE TABLE IF NOT EXISTS batcomputer.file_versions (id varchar(36) PRIMARY KEY, conversation_id varchar(36) NOT NULL REFERENCES batcomputer.conversations(id), run_id varchar(36) NOT NULL REFERENCES batcomputer.runs(id), attempt integer NOT NULL, path text NOT NULL, content text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS ix_file_versions_conversation_id ON batcomputer.file_versions(conversation_id);
CREATE INDEX IF NOT EXISTS ix_file_versions_run_id ON batcomputer.file_versions(run_id);
CREATE TABLE IF NOT EXISTS batcomputer.traces (id varchar(36) PRIMARY KEY, run_id varchar(36) NOT NULL REFERENCES batcomputer.runs(id), stage varchar(32) NOT NULL, attempt integer NOT NULL DEFAULT 0, duration_ms integer NOT NULL DEFAULT 0, input jsonb NOT NULL, output jsonb NOT NULL, error text, created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS ix_traces_run_id ON batcomputer.traces(run_id);

-- The API and worker connect to PostgreSQL directly. Browser clients must not
-- reach these internal tables through Supabase's Data API.
ALTER TABLE batcomputer.conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE batcomputer.messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE batcomputer.runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE batcomputer.plans ENABLE ROW LEVEL SECURITY;
ALTER TABLE batcomputer.reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE batcomputer.file_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE batcomputer.traces ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON batcomputer.conversations, batcomputer.messages, batcomputer.runs, batcomputer.plans, batcomputer.reviews, batcomputer.file_versions, batcomputer.traces FROM anon, authenticated;
