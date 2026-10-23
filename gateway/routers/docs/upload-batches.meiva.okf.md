---
type: doc
title: Silent upload acceptance and immutable objects
description: Keyed silent batches, durable reservations and same-body recovery.
tags: []
---

# Silent upload acceptance and immutable objects

`POST /api/agents/{id}/uploads` is the only silent save route and requires
`Idempotency-Key` plus `Idempotency-Scope: principal-v1` (`operation_key`; a missing
key or scope is 422 before any effect). The stored key includes the verified
principal, HTTP method and actual agent path. The ordered manifest fingerprints
original filenames, bytes, sizes, content types and order. Same-key changes
return 409; different keys create new objects. `deliver=true` is refused with
422 before admission: delivery is `POST /api/keyed/v1/agents/{id}/uploads`.

`gateway.routers.upload.batches` owns `agent_upload_batches`. A first writable
transaction takes a per-agent PostgreSQL xact lock, checks existing identity before
mutable agent existence, and commits the receiving manifest/reservation. A second
writable transaction takes the same lock, publishes files and commits the original
receipt. Receiving identities/reservations never expire or become fresh intents.
Ready receipts replay after target deletion: acceptance does not prove that the
historical URL is currently accessible. Fresh missing targets return 404.

The directory the files live in and the limits are values of the app's `GatewayConfig`:
`upload_home` (the account home, read once by the root), `max_upload_bytes`,
`max_agent_upload_bytes` and `max_agent_upload_files` (defaults are the constants owned by
`base.agents.upload_delivery.paths`; the root never reads them from settings, so a cluster
cannot tune a limit away). No handler reads `Path.home()` or an app-state resolver. The
decisions that need no effect, the manifest fingerprint and the object names (or the suffix
refusal), are `upload_plan.py`, strict and asserted by value; `batches.py` is the shell:
`claim_batch` commits the receiving identity, `publish_files` links the objects, and
`save_keyed_batch` runs both and commits the receipt.

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
