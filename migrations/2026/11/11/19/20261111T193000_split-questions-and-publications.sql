-- Questions and publications are separate things with content-named tables: `agent_notices`
-- holds only questions, `agent_user_messages` / `agent_user_seen` only publications, and the
-- keyed receipts serve both. Renaming keeps every row and every id (the question id is visible
-- to agents), keeps the grants (they follow the object), and renames the sequence, keys,
-- constraints and indexes with their tables so a fresh database and an upgraded one agree.
--
-- The question table is also narrowed to what its writers produce: the historical rows that
-- were not questions are deleted (nothing reads them), `question` becomes NOT NULL, and a
-- resolution other than answered / dismissed / withdrawn is rewritten to dismissed.
-- Run with the gateway, runners and upkeep stopped: the old names are not read after this.

DELETE FROM agent_notices WHERE question IS NULL;
UPDATE agent_notices SET resolution = 'dismissed'
    WHERE resolution IS NOT NULL AND resolution NOT IN ('answered', 'dismissed', 'withdrawn');

ALTER TABLE agent_notices DROP CONSTRAINT agent_notices_resolution_check;
ALTER TABLE agent_notices DROP CONSTRAINT agent_notices_question_is_object;
ALTER TABLE agent_notices DROP CONSTRAINT agent_notices_answer_requires_question;
ALTER TABLE agent_notices ALTER COLUMN question SET NOT NULL;

ALTER TABLE agent_notices RENAME TO agent_questions;
ALTER SEQUENCE agent_notices_id_seq RENAME TO agent_questions_id_seq;
ALTER INDEX agent_notices_pkey RENAME TO agent_questions_pkey;
ALTER INDEX agent_notices_open_idx RENAME TO agent_questions_open_idx;
ALTER TABLE agent_questions RENAME CONSTRAINT agent_notices_agent_id_fkey TO agent_questions_agent_id_fkey;
ALTER TABLE agent_questions RENAME CONSTRAINT agent_notices_resolution_pair TO agent_questions_resolution_pair;
ALTER TABLE agent_questions RENAME CONSTRAINT agent_notices_answered_has_reply TO agent_questions_answered_has_reply;
ALTER TABLE agent_questions ADD CONSTRAINT agent_questions_resolution_legal
    CHECK (resolution IN ('answered', 'dismissed', 'withdrawn'));
ALTER TABLE agent_questions ADD CONSTRAINT agent_questions_question_is_object
    CHECK (jsonb_typeof(question) = 'object');
ALTER TABLE agent_questions ADD CONSTRAINT agent_questions_answer_is_answered
    CHECK (answer IS NULL OR (jsonb_typeof(answer) = 'object'
                              AND resolution IS NOT DISTINCT FROM 'answered'));

ALTER TABLE agent_user_messages RENAME TO agent_publications;
ALTER SEQUENCE agent_user_messages_id_seq RENAME TO agent_publications_id_seq;
ALTER INDEX agent_user_messages_pkey RENAME TO agent_publications_pkey;
ALTER INDEX agent_user_messages_by_agent RENAME TO agent_publications_by_agent;
ALTER INDEX agent_user_messages_agent_id_source_key_key RENAME TO agent_publications_agent_id_source_key_key;
ALTER TABLE agent_publications RENAME CONSTRAINT agent_user_messages_agent_id_fkey TO agent_publications_agent_id_fkey;
ALTER TABLE agent_publications RENAME CONSTRAINT agent_user_messages_mode_check TO agent_publications_mode_check;
ALTER TABLE agent_publications RENAME CONSTRAINT agent_user_messages_suggestions_check TO agent_publications_suggestions_check;
ALTER TABLE agent_publications RENAME CONSTRAINT agent_user_messages_check TO agent_publications_check;
ALTER TABLE agent_publications RENAME CONSTRAINT agent_user_messages_content_check TO agent_publications_content_check;

ALTER TABLE agent_user_seen RENAME TO agent_publication_seen;
ALTER INDEX agent_user_seen_pkey RENAME TO agent_publication_seen_pkey;
ALTER TABLE agent_publication_seen RENAME CONSTRAINT agent_user_seen_agent_id_fkey TO agent_publication_seen_agent_id_fkey;
ALTER TABLE agent_publication_seen RENAME CONSTRAINT agent_user_seen_seen_id_check TO agent_publication_seen_seen_id_check;
COMMENT ON TABLE agent_publication_seen IS 'The user has seen publications (agent_publications.id) up to seen_id; no row means none.';

ALTER TABLE notice_operation_receipts RENAME TO keyed_operation_receipts;
ALTER INDEX notice_operation_receipts_pkey RENAME TO keyed_operation_receipts_pkey;
ALTER TABLE keyed_operation_receipts RENAME CONSTRAINT notice_operation_receipts_operation_key_check TO keyed_operation_receipts_operation_key_check;

UPDATE inbound_messages SET source = 'system:question-reply' WHERE source = 'system:notice-reply';
UPDATE inbound_messages SET source = 'system:question-dismiss' WHERE source = 'system:notice-dismiss';
