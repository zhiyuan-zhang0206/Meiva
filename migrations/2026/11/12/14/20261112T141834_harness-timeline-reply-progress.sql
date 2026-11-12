-- How far the replies of an external agent's transcript-sourced timeline have been handed to delivery. The writer
-- commits a reply to the timeline and then hands it to delivery in a step of its own; a process that dies between the
-- two must offer exactly those replies again at its next start, and no others. `replies_seq` is the highest timeline seq
-- whose reply has been through that step (or was already there when the row was made): the writer makes the row in the
-- transaction of its first commit, at the end of the timeline as it was before it, so a fork's copied history and an
-- agent's earlier event-path history are never offered. Only the runner (the agent host) reads and writes it: the
-- gateway's fork copies the timeline rows and nothing else, and a deleted agent's row goes with the agent by the
-- foreign key's cascade, which needs no grant. Expand only: a new table nothing else reads. Idempotent.
CREATE TABLE IF NOT EXISTS agent_harness_timeline_progress (
    agent_id BIGINT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    replies_seq INTEGER NOT NULL CHECK (replies_seq >= -1)
);

COMMENT ON TABLE agent_harness_timeline_progress IS
    'Per external agent: how far the replies of its transcript-sourced timeline have been handed to delivery.';
COMMENT ON COLUMN agent_harness_timeline_progress.replies_seq IS
    'Highest agent_harness_messages.seq whose reply has been handed to delivery, or was already on the timeline when the row was made (-1: none). The writer offers again the replies it wrote after it; only ever raised.';

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'meiva_runner') THEN
        GRANT SELECT, INSERT, UPDATE ON agent_harness_timeline_progress TO meiva_runner;
    END IF;
END $$;
