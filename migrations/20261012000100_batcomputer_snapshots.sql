-- Apply after 20261010011458_batcomputer_message_idempotency.sql and before
-- deploying the snapshot dual-write code. No historical rows are changed.
CREATE TABLE IF NOT EXISTS batcomputer.file_blobs (
  sha256 varchar(64) PRIMARY KEY,
  content text NOT NULL,
  size_chars integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS batcomputer.snapshots (
  id varchar(36) PRIMARY KEY,
  conversation_id varchar(36) NOT NULL REFERENCES batcomputer.conversations(id),
  run_id varchar(36) NOT NULL REFERENCES batcomputer.runs(id),
  attempt integer NOT NULL,
  parent_snapshot_id varchar(36) REFERENCES batcomputer.snapshots(id),
  manifest_sha256 varchar(64) NOT NULL,
  file_count integer NOT NULL,
  total_chars integer NOT NULL,
  criteria jsonb NOT NULL DEFAULT '[]'::jsonb,
  readme text,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ux_snapshots_run_attempt UNIQUE (run_id, attempt)
);

CREATE TABLE IF NOT EXISTS batcomputer.snapshot_files (
  snapshot_id varchar(36) NOT NULL REFERENCES batcomputer.snapshots(id),
  path text NOT NULL,
  blob_sha256 varchar(64) NOT NULL REFERENCES batcomputer.file_blobs(sha256),
  PRIMARY KEY (snapshot_id, path)
);

CREATE INDEX IF NOT EXISTS ix_snapshots_conversation_id ON batcomputer.snapshots (conversation_id);
CREATE INDEX IF NOT EXISTS ix_snapshots_run_id ON batcomputer.snapshots (run_id);

ALTER TABLE batcomputer.conversations
  ADD COLUMN IF NOT EXISTS head_snapshot_id varchar(36),
  ADD COLUMN IF NOT EXISTS candidate_snapshot_id varchar(36);

ALTER TABLE batcomputer.runs
  ADD COLUMN IF NOT EXISTS base_snapshot_id varchar(36),
  ADD COLUMN IF NOT EXISTS base_resolved_at timestamptz,
  ADD COLUMN IF NOT EXISTS result_snapshot_id varchar(36),
  ADD COLUMN IF NOT EXISTS finished_at timestamptz;

ALTER TABLE batcomputer.file_blobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE batcomputer.snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE batcomputer.snapshot_files ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON batcomputer.file_blobs, batcomputer.snapshots, batcomputer.snapshot_files
  FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT ON batcomputer.file_blobs, batcomputer.snapshots, batcomputer.snapshot_files
  TO batcomputer_app;

DO $$
DECLARE table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY['file_blobs', 'snapshots', 'snapshot_files'] LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_policies
      WHERE schemaname = 'batcomputer' AND tablename = table_name
        AND policyname = 'batcomputer_app_select'
    ) THEN
      EXECUTE format('CREATE POLICY batcomputer_app_select ON batcomputer.%I FOR SELECT TO batcomputer_app USING (true)', table_name);
    END IF;
    IF NOT EXISTS (
      SELECT 1 FROM pg_policies
      WHERE schemaname = 'batcomputer' AND tablename = table_name
        AND policyname = 'batcomputer_app_insert'
    ) THEN
      EXECUTE format('CREATE POLICY batcomputer_app_insert ON batcomputer.%I FOR INSERT TO batcomputer_app WITH CHECK (true)', table_name);
    END IF;
  END LOOP;
END $$;
