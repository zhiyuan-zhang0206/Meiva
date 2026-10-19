---
type: doc
title: meiva.files — File System
description: Persistent file operation interface for agents. Relative paths resolve to the agent's workspace, `~/...` resolves to the user home.
tags: []
---

# meiva.files — File System

## What it is

Persistent file operation interface for agents. Relative paths resolve to the agent's private workspace directory (each agent independent). `~/...` resolves to the user home. (The `meiva_code` plugin overlays mutable `meiva.cwd` tracking on top; core baseline always uses the workspace, never silently falls back to `$HOME`.)

## Core API

- `read(path, start=None, end=None, *, limit=None, with_line_numbers=False) → str` — read a file, supports 1-indexed line ranges. `limit` and `end` are mutually exclusive. Read content passes through injection scanning (clean content returned as-is).
- `write(path, content)` — overwrite, auto-creates parent directories. The write is atomic: the content goes to a temporary file in the same directory that then replaces the target (permission bits of an existing file are kept, a read-only file is refused), so a concurrent reader never sees a half-written file and a failed write leaves the old file and no temporary behind.
- `append(path, content)` — append, creates if not exists.
- `edit(path, old, new, *, replace_all=False)` — exact replacement. `old` defaults to matching once only; `replace_all=True` replaces all occurrences. On no match, error returns a diff hint of the closest match.
- `delete(path)` — move a file to the trash (does not delete directories, and never unlinks).
- `restore(path) → Path` — move a trashed file back to the path it was deleted from.
- `glob(pattern='*') → list[Path]` — glob matching (supports `**` recursion).

## Trash

A delete is recoverable. The file leaves its path at once and lands under
`$MEIVA_HOME/trash/<machine>/<YYYY-MM-DD>/<original absolute path without its filesystem root>`
— `/home/me/notes.txt` deleted on 2026-10-19 on machine `macmini` sits at
`~/.meiva/trash/macmini/2026-10-19/home/me/notes.txt`. The layout names the origin
itself, so recovery needs no manifest:

- **Same path twice in one day**: the day bucket moves on (`2026-10-19.2`,
  `.3`, …), so an earlier entry is never overwritten and the payload keeps its
  exact origin path.
- **Machine layer**: `MEIVA_MACHINE_NAME`, the deployment label (never the
  hostname); an uninitialized home uses `local`. The tree is per home, so the
  layer is provenance, not routing.
- **Restore**: `meiva.files.restore(<path inside the trash>)` moves the entry back
  to its origin and refuses when something exists there again — it never
  overwrites. Reading the trash directory is enough to find the path.
- **Retention**: entries older than `MEIVA_TRASH_RETENTION_DAYS` (default 30) are
  purged by the agent-host's boot maintenance, next to the trace-mirror prune.
  The pass only reads and removes inside the trash tree; a symlink in it is
  removed as a link, never followed.
- **Same filesystem**: one atomic rename. Across devices it copies to a temporary
  sibling, flushes it, renames it into place, and only then unlinks the original,
  so a failure never leaves a half-copied entry with the original gone.

Framework temporary files (`.exec_output`, sockets, locks) are still unlinked
directly: they are not user data and must not accumulate in the trash.

## Constraints
- Relative paths resolve to the agent's workspace (each agent independent)
- `edit()` does not allow fuzzy matching — `old` must be exact
- Does not operate on directories (`delete` only deletes files)

## Key Dependencies
- [[meiva/self/docs/self.meiva.okf.md|self]] — workspace belongs to the agent itself, `meiva.self` carries identity
