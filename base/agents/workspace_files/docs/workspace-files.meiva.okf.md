---
type: doc
title: Workspace files
description: '`base/agents/workspace_files/` — the one reader for an agent''s workspace file: which directories may answer a path, what a refusal class means, and why the same code runs on the machine that owns the disk and on the gateway that serves it.'
tags:
- base
- agents
- workspace
---

# Workspace files

A workspace file is whatever an agent wrote and a reader wants back: the panel's file viewer opens
it, and a link in a transcript points at it. Two machines can answer for it — the one that owns the
disk, and the gateway in front of them — and a file must mean the same thing on both, so the
resolution and the read live here and nowhere else.

## What may answer a path

`allowed_roots` returns two things: the directories a path may resolve under, and the primary one.
The agent's **cwd** is primary, because a relative path in a transcript was relative to the turn that
printed it. The agent's **workspace** is the fallback, because that is where a link from the panel
lands. A cwd that no longer exists (a deleted worktree, a moved checkout) leaves the workspace alone.

`resolve_workspace_file_path` then accepts three shapes: a relative path, an absolute path, and a
`file://` URL with an optional `#L12-L15` or `?x=1` suffix. Suffixes are stripped before anything
else, because neither names a file. An absolute path is honoured only when it resolves inside a root;
otherwise it is retried as if it were relative, because a client may replay a path printed on one
machine against a different mount.

## Refusals

A refused read raises `WorkspaceFileRefused` carrying a `WorkspaceFileFailure`:

| class | HTTP | meaning |
|---|---|---|
| `path_invalid` | 400 | empty, or carrying a null byte — nothing can resolve it |
| `outside_roots` | 403 | resolved outside every root |
| `not_found` | 404 | inside a root, and not a file |
| `too_large` | 413 | a text read over the byte budget; the raw route still serves it |

The class is the status. The gateway forwards it unchanged when it asks another machine, so the two
sides cannot disagree about what "not found" means and no adapter translates a reason string back
into a number.

## Two reads

`read_workspace_file` answers the viewer's document: the text, or `binary=true` with no text when the
extension or the media type says the file is rendered rather than read, and a 1 MB budget for the
text case. `read_workspace_file_bytes` answers the raw route: no decode, no extension judgement, and a
budget only when the transport names one.

Both report `path` relative to the root they found, so a reader never learns where on disk an agent's
home is.

## Which machine answers

The gateway reads locally when the agent's `agents_meta.machine` is its own. Otherwise it asks that
machine's `agent-ops` over `POST /workspace-file/content` and `POST /workspace-file/raw`, which call
this same reader under an `_owned_agent` check — a stale routing row must not serve a same-named file
from the wrong disk.
