---
type: doc
title: Guarded delivered upload retry surface
description: Durable source acceptance, native immutable copy and retained inbound outcome.
tags: [gateway, idempotency]
---

# Guarded delivered upload retry surface

| Route | Retry policy | Evidence |
| --- | --- | --- |
| `POST /api/keyed/v1/agents/{agent_id}/uploads/staged` | keyed / transactional | required verified principal-v1/key; per-file staging in `.staged-v1/` and `upload_staged_files`, replay returns the original staged receipt |
| `DELETE /api/keyed/v1/agents/{agent_id}/uploads/staged/{file_id}` | natural | unreferenced staged file deletion (204); 409 if referenced by a delivery batch |
| `POST /api/keyed/v1/agents/{agent_id}/uploads` | keyed / transactional | required verified principal-v1/key; immutable source acceptance (from `file_ids` + `text` or multipart), versioned create-only copy and retained native inbound outcome |

Browser file and image uploads stage each file immediately via `/uploads/staged` and
submit one batch/key to `/api/keyed/v1/agents/{agent_id}/uploads`, validating its
202 source receipt. SDK/MCP consumers are not activated.
Old consumers cannot effect the new path/kind. 202 reports historical source acceptance, not remote readiness or
agent execution. Copy is attempted at most three times; retained status distinguishes
pending retries from inspectable HOLD, and a HOLD is not automatically retried (the
composer offers Restore to staged attachments or explicit Retry with a new batch key).

Current owner: [delivered uploads](../../../upload_delivery/docs/delivered-uploads/delivered-uploads.meiva.okf.md).
Saved-file serving: [upload batches](../../../routers/docs/upload-batches.meiva.okf.md).
