-- Remove legacy pages subsystem: triggers, functions, and tables
DROP TRIGGER IF EXISTS agents_meta_terminate_cascade_pages ON agents_meta;
DROP TRIGGER IF EXISTS agents_meta_resurrect_cascade_open_pages ON agents_meta;
DROP TRIGGER IF EXISTS cascade_close_agent_pages ON agents_meta;
DROP TRIGGER IF EXISTS cascade_open_agent_pages ON agents_meta;
DROP TRIGGER IF EXISTS cascade_close_agent_pages ON agents;
DROP TRIGGER IF EXISTS cascade_open_agent_pages ON agents;

DROP FUNCTION IF EXISTS cascade_close_agent_pages();
DROP FUNCTION IF EXISTS cascade_open_agent_pages();

DROP TABLE IF EXISTS page_operation_receipts CASCADE;
DROP TABLE IF EXISTS agent_pages CASCADE;
