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
a present unreadable file propagates its I/O error. Invalid JSON, a non-object
value and invalid Pydantic state raise rather than falling back to all-enabled
and rewriting evidence of corruption. This is the same state-loss boundary as
`installed.json`.

Load validates names against the supplied discovered set, folds human dash names
to their real underscore directory spellings, and adds newly discovered names as
enabled in memory. Strict callers reject dangling names. Runtime callers drop
those entries, treat them as disabled and report each name through the required
reporter on every failed load; recovery never makes an unknown name enabled.

Local enable updates first resolve the requested name against the actual local
set. Unknown names fail before a write. The written image contains only plugins
present on this machine, so stale entries cannot make the next strict read fail.
Keys are written in sorted order because image iteration controls registration
order. Writes replace the complete local image; this operation does not update
a database or remote authority.
