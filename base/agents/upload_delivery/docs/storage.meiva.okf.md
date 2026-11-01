---
type: doc
title: Immutable upload storage and native identity
description: Strict manifests, protocol proof and physical-root receiving quota.
tags: [base, uploads]
---

# Immutable upload storage and native identity

`models` owns staged-file records (`StagedFile`), ordered immutable manifests, source
acceptance, mutable delivery status (`message_text`, `files` with `staged_id`) and
independently versioned native request/proof (`final_names`). Protocol 1 is required
as an actual integer; omission, bool, float and unknown values fail before effects.
Unit identity reuses `base.host.db_authority.unit.UnitIdentity`; native machine and
resolved `MEIVA_HOME` identify the receiver, not the physical uploads quota owner.
`naming` owns filename sanitation (`sanitize_upload_filename`), batch/workspace
collision disambiguation (`disambiguate_filename`) and safe suffix extraction.
`staging` owns per-file staged uploads (`upload_staged_files` and `.staged-v1/`
files), keyed idempotent acceptance and unreferenced staged-file deletion.

`paths` owns the shared upload URL, path, image MIME, limits and safe-serving
helpers used by gateway, Ops and inbound image consumers. Two directories, named by
the same owner: `agent_upload_dir` is the gateway's staging directory for one agent
(`<MEIVA_HOME>/uploads/<agent_id>` unless `MEIVA_UPLOAD_STAGING_ROOT` moves the root),
which `source.accept` and `source.accept_from_staged` receive as `upload_root`;
`delivered_upload_dir` is the copy the agent reads (`<that machine's MEIVA_HOME>/workspaces/<agent_id>/<configured
name>`), which only a receiver builds and always from the home of the machine the
batch targets. Staging is internal state and never a user-visible folder. Its image fetch
dials the gateway with the same auth headers as every other agent -> gateway call
(`base.cluster.machine.gateway_auth_headers`): the delivered machine API token
when the process carries one, the cluster secret otherwise.
`storage` uses the shared agent xact gate and counts flat files, legal nested final
objects, staged files and receiving reservations for actual machine/resolved directory. Source
and copy manifests sharing a physical root count each batch path once. Primitive dot-temps are
held staging, excluded from final inventory and never swept by this owner.

Hidden `.delivered-v1/{batch}/{UUID-ordinal-suffix}` objects and human-readable
workspace copies (`<delivered_upload_dir>/<final_name>`) publish through native
`create_private_bytes`: no overwrite, only matching size/hash recovery, then fsync.
Verified manifest paths cannot escape native root. Suffix/type/name validation
precedes reservation; basenames reserve 38 bytes for the native temporary name.
Original display filenames are not physical paths, and carry no control characters:
the delivered-batch system message echoes each one verbatim as a `- name: path`
line, so a newline in a name would forge a line of its own. The intake boundary
refuses one with a 422 and `Object.filename` refuses it again when a stored manifest
is replayed (task #5855).

`source` owns retained acceptance and inbound outcome (`system:upload` carrying
`meiva_uploaded_files` in `inbound_meta`, plus an optional companion `user` chat
inbound when `message_text` is non-empty) with no TTL/cleanup FK.
Accepted outcome is consulted before mutable target facts; HOLD cannot accept a
late proof. New placement and inbound acceptance share locks and transaction.
See the [Gateway domain contract](../../../../gateway/upload_delivery/docs/delivered-uploads/delivered-uploads.meiva.okf.md)
for source rounds, remote Ops proof and authentication.

Upload directory and traversal helpers receive their root explicitly (the staging
root, or the delivered machine's home); a request root binds that path once before
giving a two-argument resolver to a consumer.
Image fetch receives the existing gateway HTTP client through its narrow
`UploadClient` contract. The hosted claim root passes the context's lazy client
factory, so text and invalid image references do not construct a gateway client.
The fetch resolves that factory only after its canonical image-MIME guard.
The actual process client retains its bearer and IPv4 transport, and each image
request retains the fifteen-second timeout and HTTP-error-to-OSError boundary.
Tests drive real httpx request and response handling through `httpx.MockTransport`: the answer
comes from the test and not from a listener on port 0, which the
[test-seam convention](../../../../docs/conventions/python-conventions.md#test-seams-in-another-shape)
asks a new test to prefer.
