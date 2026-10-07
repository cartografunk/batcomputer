-- Apply to Supabase/PostgreSQL before starting the API.
CREATE TABLE IF NOT EXISTS conversations (id varchar(36) PRIMARY KEY, owner_hash varchar(64) NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS ix_conversations_owner_hash ON conversations(owner_hash);
CREATE TABLE IF NOT EXISTS messages (id varchar(36) PRIMARY KEY, conversation_id varchar(36) NOT NULL REFERENCES conversations(id), role varchar(16) NOT NULL, content text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS ix_messages_conversation_id ON messages(conversation_id);
CREATE TABLE IF NOT EXISTS runs (id varchar(36) PRIMARY KEY, conversation_id varchar(36) NOT NULL REFERENCES conversations(id), message_id varchar(36) NOT NULL REFERENCES messages(id), status varchar(24) NOT NULL DEFAULT 'queued', result text, attempts integer NOT NULL DEFAULT 0, api_retries integer NOT NULL DEFAULT 0, error text, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), heartbeat_at timestamptz, lease_id varchar(36));
CREATE INDEX IF NOT EXISTS ix_runs_conversation_id ON runs(conversation_id);
CREATE INDEX IF NOT EXISTS ix_runs_status ON runs(status);
CREATE TABLE IF NOT EXISTS plans (id varchar(36) PRIMARY KEY, run_id varchar(36) NOT NULL REFERENCES runs(id), content jsonb NOT NULL);
CREATE INDEX IF NOT EXISTS ix_plans_run_id ON plans(run_id);
CREATE TABLE IF NOT EXISTS reviews (id varchar(36) PRIMARY KEY, run_id varchar(36) NOT NULL REFERENCES runs(id), attempt integer NOT NULL, content jsonb NOT NULL);
CREATE INDEX IF NOT EXISTS ix_reviews_run_id ON reviews(run_id);
CREATE TABLE IF NOT EXISTS file_versions (id varchar(36) PRIMARY KEY, conversation_id varchar(36) NOT NULL REFERENCES conversations(id), run_id varchar(36) NOT NULL REFERENCES runs(id), attempt integer NOT NULL, path text NOT NULL, content text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS ix_file_versions_conversation_id ON file_versions(conversation_id);
CREATE INDEX IF NOT EXISTS ix_file_versions_run_id ON file_versions(run_id);
CREATE TABLE IF NOT EXISTS traces (id varchar(36) PRIMARY KEY, run_id varchar(36) NOT NULL REFERENCES runs(id), stage varchar(32) NOT NULL, attempt integer NOT NULL DEFAULT 0, duration_ms integer NOT NULL DEFAULT 0, input jsonb NOT NULL, output jsonb NOT NULL, error text, created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS ix_traces_run_id ON traces(run_id);

-- The API and worker connect to PostgreSQL directly. Browser clients must not
-- reach these internal tables through Supabase's Data API.
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE plans ENABLE ROW LEVEL SECURITY;
ALTER TABLE reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE file_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE traces ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON conversations, messages, runs, plans, reviews, file_versions, traces FROM anon, authenticated;
