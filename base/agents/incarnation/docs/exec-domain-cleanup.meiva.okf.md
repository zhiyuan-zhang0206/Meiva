---
type: doc
title: Exec domain directory cleanup
description: The one supported way to inspect and remove settled managed-exec run directories, and why the run tree is never globbed.
tags:
- base
- exec
- run-dir
---

# Exec domain directory cleanup

`<home>/run/exec/<agent>/domains/<request>/` is one managed exec run: the
request envelope, the owner messages, the result and the close receipt. The
owner that writes it (`agent/graph/exec/_owned_run.py`) removes it as part of
settlement; what survives belongs to a parent that died before it settled, or to
a run nobody has cleaned up since.

`python -m base.agents.incarnation.exec_domain_cleanup --agent <id>` reports
them and removes nothing. `--all-agents` covers every agent directory under the
home, `--older-than-hours` moves the retention margin, and `--delete` is the only
destructive switch. The run must happen against a configured home, because the
proof comes from the database.

## The rule

A directory is **settled** — removable — only when all of these hold:

- the request id is absent from the agent row's stored allocation set
  (`agents_meta.incarnation_resources.requests`, owned by
  `base/agents/incarnation/resources.py`). `register_exec` inserts the
  allocation before the child exists and `complete_exec` deletes it after the
  owner's close receipt, so an absent id cannot still be running under a live
  incarnation;
- no live process names the envelope through `MEIVA_EXEC_REQUEST_FILE` (the
  direct child and every env-inheriting descendant carry it);
- the directory is older than the retention margin;
- the directory has exactly the written shape: `<uuid>/req-<uuid.hex>.json`.

Anything unprovable is retained and reported instead: an unreadable resource
set, a directory whose request id is still allocated, a live reference, a
directory too young, a symlink, or a file set the writer never produces. The
report lists every retained directory with its reason, so a pass never leaves a
silent remainder.

## Never glob the run tree

The request envelope is the crash-stable half of a running exec, and an
unrelated pass that deletes it wedges the run it belonged to — issue #5362 was a
glob that hit the caller's own envelope, and a cross-agent glob widens that
radius to every agent on the machine. Use this pass instead; it refuses exactly
the directories that a glob cannot tell apart.

## Where the directory belongs

The directory stays in the home-owned run tree and not in the agent's writable
workspace: it is the child's handoff and the parent's crash-stable evidence, and
an agent that could rewrite it could erase the proof its own recovery depends
on. The gap this pass closes is the missing removal path, not the location.
