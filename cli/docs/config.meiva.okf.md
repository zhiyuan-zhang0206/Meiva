---
type: doc
title: Config CLI
description: '`meiva config` is the gateway config client and a settings-free local `.env` repair surface.'
tags:
- tool
- configuration
---

# Config CLI

`cli/commands/management/config.py` implements `meiva config get/set/unset`. The normal
path is a thin client for `GET/PUT /api/config`: it resolves the gateway URL
and a bearer (a delivered machine API token, the gateway's human secret, or a
remote unit's capability token) from the process environment or unit files, then
sends only the requested merge-patch delta. It never restarts processes; the
gateway response names the restart targets.

`--local` operates directly on this unit's `$MEIVA_HOME/.env` and never dials the
gateway. It reads aliases, sensitivity, scope, editability, type, choices, and
restart metadata from `base.config.config_registry`; sensitive values are masked.
Before writing, it validates the full affected candidate through
`base.config.admin.candidate`, so a cross-field-invalid patch cannot replace the
only local config file. Host fields are locally writable; a pure runner cannot
write cluster fields locally because its cluster configuration is fetched from
the gateway.

Keys that decide who authenticates to the cluster are read-only on every config write path (the API, the ops op and
`--local`): `MEIVA_CLUSTER_SECRET` rotates only through
`scripts/data_plane_ops/rotate_cluster_secret.py`, and
`MEIVA_ALERTS_WEBHOOK_TOKEN` change only by editing the gateway `.env` on its host.
Otherwise any authenticated caller, a machine token included, could pick
a credential that outlives its own admission. Writable secrets are outbound
credentials (provider, search, chat and backup-store keys) only;
`base/config/tests/test_config_editing.py` pins that classification.

## Client contract evidence

The config CLI tests use a local HTTP listener and isolated child environments.
They exercise the actual GET/PUT wire models, alias lookup, type coercion,
merge-patch delta, explicit-null unsets, remote editability, restart hints and
audited sensitive-value withholding. A home with no gateway identity or a
conflicting environment override must issue no HTTP request when writing.
List values traverse the real runtime writer and settings parser. Local repair
retains unrelated lines, masks credential paths and validates the full candidate
before replacing its file. The invalid timeout pair is checked in a fresh child
with no inherited timeout aliases, and its rejection preserves the original bytes.
The lazy import and incident-repair cases keep running without a valid Settings
image. Local audit checks retain newest-first ordering and per-alias filtering.
