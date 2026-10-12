-- Apply before deploying code that reads these columns.
-- Existing messages keep NULL keys and remain valid.
ALTER TABLE batcomputer.messages
  ADD COLUMN IF NOT EXISTS client_message_id varchar(64),
  ADD COLUMN IF NOT EXISTS request_hash varchar(64);

CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_idempotency
  ON batcomputer.messages (conversation_id, client_message_id)
  WHERE client_message_id IS NOT NULL;
