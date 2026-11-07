---
type: doc
title: Local plugin enable images
description: Explicit paths, discovery and fail-fast updates of machine-local plugin enable state.
tags: [base, plugins]
---

# Local plugin enable images

The machine's `plugins_config.json` controls which plugin faces are imported at
startup. It is separate from environment-driven Core Settings. Builtin and
external plugins share one name map; their distinction belongs to discovery:
builtin roots come from the repository and external roots from the selected unit
home. The explicit scanner rejects a duplicate name and ignores dot-prefixed
staging or backup directories left by interrupted atomic installation.

`PluginEnableStore` owns a required file path. Canonical process functions select
the normal path and delegate to the same store. Reads never persist default or
newly discovered entries. An absent or empty image enables all known plugins;
a present unreadable file propagates its I/O error. Invalid JSON and non-object values raise rather than falling back to
all-enabled and rewriting evidence of corruption. Missing or falsey `plugins`
entries retain the previous all-enabled behavior; nonempty entries undergo
Pydantic validation. This preserves the existing state-loss boundary of
`installed.json` without widening schema admission in this refactor.

Load validates names against the supplied discovered set, folds human dash names
to their real underscore directory spellings, and adds newly discovered names as
enabled in memory. Strict callers reject dangling names. Runtime callers drop
those entries, treat them as disabled and report each name through the required
reporter on every failed load; recovery never makes an unknown name enabled.

`RETIRED_BUILTIN_PLUGINS` names the built-in plugins a release deleted. Their stale
entries are dropped and reported like any dangling name, but `meiva plugins verify`
shows them as a `WARN` rather than a failure; the PR that deletes a built-in plugin
adds its name. Any other missing plugin stays a failure.

Local enable updates first resolve the requested name against the actual local
set. Unknown names fail before a write. The written image contains only plugins
present on this machine, so stale entries cannot make the next strict read fail.
Keys are written in sorted order because image iteration controls registration
order. Writes replace the complete local image; this operation does not update
a database or remote authority.

## Cluster-managed plugins

The gateway initializes plugin decisions in `extensions.default_enabled` and serves
GET/PUT `/api/plugins`. `base/packages/extensions/plugin_enable.py` owns that policy
and its startup projection. `plugin_enable_policy.json` is an atomic runtime cache,
not a local authority: every cached decision overrides the legacy image, including
an explicit enable over a local disable. Unknown-to-this-home policy names stay in
the cache and take effect when their content becomes available.

The existing local CLI and host inventory writer refuse managed names. The Plugins
UI saves once for the cluster without runner RPC, and announces restart activation.
Agent-host boot and converge refresh the cache from the registry; a failed refresh
retains it and warns. See
[Cluster plugin enablement](../../../../../docs/decisions/extensions/plugins/2026-11-07-cluster-plugin-enablement.md).

Cache publication takes the per-home `plugin_enable_policy.lock` before reading
registry policy and atomically replacing the image. Startup, converge and API
refresh share this owner, so an earlier refresh cannot publish an old snapshot
after a later writer. API policy transactions commit before cache publication;
a publication failure retains the committed policy and reports refresh failure.
