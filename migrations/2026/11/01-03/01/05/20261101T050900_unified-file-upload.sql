-- Every file a person picks, pastes or drops in the web composer goes through one path: its
-- bytes are staged on the gateway as the person picks it (`upload_staged_files`), and pressing
-- send freezes a batch from the staged files and the person's text. The batch lands in the
-- agent's workspace as `<upload dir>/<send time>/<original name>` and its accepted outcome is
-- two chat inbounds committed together: the file note, then the person's message.
--
-- Protocol 2 batches carry the send directory and the frozen message. A protocol-1 batch
-- (the earlier `.delivered-v1/<batch>/<uuid>-<n>` layout, accepted from a multipart upload)
-- cannot be copied by this code, so one still waiting for its copy is held; its stored
-- manifest and any file it already delivered stay exactly as they are.
--
-- IF NOT EXISTS throughout because `db/schema.sql` already declares every object: a fresh
-- database builds from there and then replays the post-baseline files it has not stamped
-- (migrations/README.md).
CREATE TABLE IF NOT EXISTS upload_staged_files (
    file_id text PRIMARY KEY CHECK (file_id ~ '^[0-9a-f]{32}$'),
    operation_key text NOT NULL UNIQUE,
    agent_id bigint NOT NULL CHECK (agent_id > 0),
    request_hash text NOT NULL CHECK (length(request_hash) = 64),
    name text NOT NULL,
    size bigint NOT NULL CHECK (size >= 0),
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    content_type text NOT NULL,
    source_unit jsonb NOT NULL,
    storage_machine text NOT NULL,
    storage_directory text NOT NULL,
    state text NOT NULL DEFAULT 'receiving' CHECK (state IN ('receiving', 'ready', 'released')),
    batch_id text REFERENCES upload_delivery_batches (batch_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    released_at timestamptz,
    CHECK ((state = 'released') = (released_at IS NOT NULL)),
    CHECK (state <> 'released' OR batch_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS upload_staged_unsent
    ON upload_staged_files (created_at) WHERE batch_id IS NULL;
CREATE INDEX IF NOT EXISTS upload_staged_batch
    ON upload_staged_files (batch_id) WHERE batch_id IS NOT NULL;
COMMENT ON TABLE upload_staged_files IS
    'Files a person picked for their next send: bytes staged on the gateway, taken by at most one live batch. An untaken row and its bytes are swept after the staged-file TTL; a taken one keeps its row as evidence after the accepted batch''s bytes are deleted.';

ALTER TABLE upload_delivery_batches
    ADD COLUMN IF NOT EXISTS protocol smallint NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS send_directory text,
    ADD COLUMN IF NOT EXISTS message jsonb;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'upload_delivery_batches_protocol_check'
    ) THEN
        ALTER TABLE upload_delivery_batches
            ADD CONSTRAINT upload_delivery_batches_protocol_check CHECK (
                protocol IN (1, 2)
                AND (protocol = 1 OR (send_directory IS NOT NULL AND message IS NOT NULL))
            );
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS upload_delivery_send_directory
    ON upload_delivery_batches (agent_id, send_directory) WHERE send_directory IS NOT NULL;

COMMENT ON COLUMN upload_delivery_batches.protocol IS
    '1: the earlier multipart-upload layout, never copied by the current code. 2: a send of staged files with its frozen message and send directory.';

-- Only the state changes: a protocol-1 batch that is still waiting for its copy would
-- otherwise stay pending forever, because no worker of this code claims it.
UPDATE upload_delivery_batches
    SET state = 'hold', reason = 'superseded-by-unified-upload'
    WHERE protocol = 1 AND state = 'pending';

COMMENT ON TABLE agent_upload_batches IS
    'Historical silent native-image upload identities (nothing writes one now); receiving manifests keep reserving quota without expiry, ready receipts replay acceptance.';
