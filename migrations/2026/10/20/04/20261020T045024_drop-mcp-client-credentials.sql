-- The gateway `/mcp` endpoint is a thin forwarder onto the HTTP API and
-- authenticates like any API request
-- (decisions/data/security/2026-10-20-mcp-is-a-thin-api-forwarder.md), so the
-- MCP client token table and its credential-creation receipts have no reader
-- or writer left. Issued MCP client tokens stop working.

DROP TABLE IF EXISTS mcp_credential_creation_receipts;
DROP TABLE IF EXISTS mcp_clients;
