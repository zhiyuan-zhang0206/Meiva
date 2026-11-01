---
type: doc
title: Silent upload acceptance and immutable objects
description: Keyed silent batches, durable reservations and same-body recovery.
tags: []
---

# Silent upload acceptance and immutable objects

`GET /api/agents/{id}/uploads/{filename}` serves an uploaded file by its sanitized
or disambiguated filename (`gateway/routers/upload/router.py`). It resolves the
agent's workspace delivered upload directory (`delivered_upload_dir`) first and falls
back to the gateway staging directory (`agent_upload_dir`), refusing path traversal
and serving with `X-Content-Type-Options: nosniff`.
Staged file upload (`POST /api/keyed/v1/agents/{id}/uploads/staged`), staged file
deletion (`DELETE /api/keyed/v1/agents/{id}/uploads/staged/{file_id}`) and batch
delivery (`POST /api/keyed/v1/agents/{id}/uploads`) live in
`gateway/upload_delivery/router.py`. See the
[delivered upload owner](../../upload_delivery/docs/delivered-uploads/delivered-uploads.meiva.okf.md).

`gateway.routers.upload.batches` retains the lower-level `agent_upload_batches`
helpers for legacy batch records. A first writable
transaction takes a per-agent PostgreSQL xact lock, checks existing identity before
mutable agent existence, and commits the receiving manifest/reservation. A second
writable transaction takes the same lock, publishes files and commits the original
receipt. Receiving identities/reservations never expire or become fresh intents.
Ready receipts replay after target deletion: acceptance does not prove that the
historical URL is currently accessible. Fresh missing targets return 404.

The directory the files live in and the limits are values of the app's `GatewayConfig`:
`upload_staging_root` (the staging root — `<MEIVA_HOME>/uploads` unless
`MEIVA_UPLOAD_STAGING_ROOT` names another, resolved once by the root through
`base.paths.uploads_path`), `max_upload_bytes`, `max_agent_upload_bytes` and
`max_agent_upload_files` (defaults are the constants owned by
`base.agents.upload_delivery.paths`; the root never reads them from settings, so a cluster
cannot tune a limit away). A silent save's bytes stay in staging: the agent fetches them
by URL, and the copy it reads on disk is the delivered one in its own workspace. No handler reads `Path.home()` or an app-state resolver. The
decisions that need no effect, the manifest fingerprint and the object names (or the suffix
refusal), are `upload_plan.py`, strict and asserted by value; `batches.py` is the shell:
`claim_batch` commits the receiving identity, `publish_files` links the objects, and
`save_keyed_batch` runs both and commits the receipt. The batch id and each attempt's
staging id are values: the router draws them from the config's `rng` and hands them to
`save_keyed_batch`, which passes them to the two steps.

Files use flat `meiva-upload-<uuid>-<ordinal><suffix>` names; original names remain
display metadata. Suffixes preserve image MIME consumers and must fit 128 UTF-8
bytes without URL delimiters `?`, `#`, `%` or control characters; NUL in filenames
is refused. An absent suffix is valid. Names stay
below filesystem limits without truncating long display names. A caller-chosen
name never becomes a final name, so no writer can overwrite an existing object.

Each attempt owns separate hidden staging. Existing `create_private_bytes` fsyncs
complete staging files; create-only hard links publish final objects. Existing
objects must match size/hash. Final directories are fsynced before the ready
receipt commits. Recovery validates present objects and fills missing ones. A
disconnected old writer can only publish identical immutable bytes; no attempt
replaces or deletes finals or cleans another attempt's staging.

Quota admission uses `base.agents.upload_delivery.storage` for shared native physical-root inventory, including delivered hidden finals/reservations. It counts receiving manifests once, excluding their already-published
finals from the disk baseline. Orphan staging and abandoned receiving
reservations have no automatic cleanup; recovery needs the same key and bytes.
Receiving identities must not be pruned to release quota.

Browser silent uploads mint one key per `uploadFiles` invocation or accept an
explicit reusable key. Cross-call recovery requires retaining that key and files;
there is no browser outbox, persistence or automatic ambiguous retry. Upload
progress measures transferred bytes, not committed acceptance. The separate [guarded delivered owner](../../upload_delivery/docs/delivered-uploads/delivered-uploads.meiva.okf.md) closes remote-copy and one-chat recovery without activating these consumers.
