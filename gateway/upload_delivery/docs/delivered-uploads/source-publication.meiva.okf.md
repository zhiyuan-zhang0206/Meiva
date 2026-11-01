---
type: doc
title: Delivered upload source publication and quota
description: Original physical identity, immutable file publication and shared native quota admission.
tags: [gateway, uploads]
---

# Delivered upload source publication and quota

`base.agents.upload_delivery.source` owns `upload_delivery_batches` and
`base.agents.upload_delivery.staging` owns `upload_staged_files`.
`POST /api/keyed/v1/agents/{id}/uploads/staged` stages one file under `principal-v1`
and `Idempotency-Key` into `.staged-v1/{agent_id}/{file_id}`;
`DELETE /api/keyed/v1/agents/{id}/uploads/staged/{file_id}` removes an unreferenced
staged file (409 when referenced by a batch). Tx A takes
the existing agent upload xact gate, consults retained key identity **before**
mutable target existence, disambiguates human-readable workspace filenames
(`naming.disambiguate_filename`), then freezes source/target `UnitIdentity`, manifest,
physical root and verified `InboundProvenance`. When accepting from staged files
(`accept_from_staged`), Tx A locks the referenced `upload_staged_files` rows and
links their `.staged-v1/` bytes into `.delivered-v1/{batch}/{object}`. The target is the unique serving
runner unit whose URL matches the machine's registered Ops address. Receiver
compares actual machine/home; a changed registry URL cannot silently redirect a
batch to another home. Unbound or ambiguous fresh placement rejects before claim.

Source receiving files are published outside a borrowed DB connection with
`create_private_bytes`, create-only verification and directory fsync. Tx B commits
original 202 acceptance and pending delivery intent together. Receiving identity
and reservation never expire; interrupted source publication needs original key
and bytes. Ready replay precedes mutable target/deletion checks. No cleanup of
finals, unbound staging or other attempts is performed.

Source finals use the gateway's staging root:
`{MEIVA_HOME}/uploads/{id}/.delivered-v1/{batch}/{object}` by default, or
`MEIVA_UPLOAD_STAGING_ROOT/{id}/...` when that is set. Staging is this unit's
internal state, not a user-visible folder, and never the agent's workspace: the
copy an agent reads is the receiver's (`<that machine's MEIVA_HOME>/workspaces/
{id}/MEIVA_UPLOAD_DIR_NAME/.delivered-v1/{batch}/{object}`). `storage.check_quota` serializes through the existing agent xact
gate and counts actual flat files, legal hidden finals and receiving reservations
bound to machine/resolved directory. Because reservations are matched by that
directory, changing `MEIVA_UPLOAD_DIR_NAME` starts a fresh account: copies under the
old name stay where they are and no longer count against the agent's quota. Same batch paths count once across source
and receiver homes. Old silent receiving rows have no physical binding and are
conservatively retained in accounting, never assigned or deleted by inference.
Native primitive dot-temp files are staging; hard-crash leftovers are held and
excluded beside manifest reservation, without erasing them. Unknown flat
files count normally. Silent saves and remote copies admit through this one quota owner.

The authenticated manifest-bound GET `.../uploads/{batch}/objects/{ordinal}`
serves only ready source-owned objects, after hash/size verification. It accepts
no arbitrary filesystem path, survives historical agent-row deletion, sends
attachment/nosniff headers and is separate from old native-image upload URLs.
