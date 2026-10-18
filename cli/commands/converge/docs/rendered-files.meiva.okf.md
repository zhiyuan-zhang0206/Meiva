---
type: doc
title: Converge-rendered files
description: The drift contract for converge-managed copies — when a renderer restores its source and when it preserves a local edit.
tags: [cli, converge]
---

# Converge-rendered files

Converge writes some destinations from a source the repository owns: the Grafana
provisioning tree, the `meiva-ops` dashboard, the otel-collector config, extension
materializations and skill copies. Each write records the sha256 of what converge
put there (`provisioning-hashes.json`, `rendered-hashes.json` or the extension
registry). A destination whose current hash differs from that record changed
outside converge, and the renderer decides what to do from **who owns the
surface**, named by `on_local_edit` in `rendered_file.write_rendered_guarded`
(`cli/commands/converge/rendered_file.py`):

- `preserve` — a surface a person may own. Converge keeps the local content,
  prints the warning and emits `converge_file_preserved`; the recorded hash stays
  untouched, so the warning repeats every converge until the file is removed.
- `overwrite` — a repository-owned surface. Converge restores the rendered
  content, emits `converge_file_overwritten`, and the record follows the restore.

The Grafana provisioning tree is `overwrite`: rules are configuration as code, and
a locally kept variant runs rules the checkout no longer declares. This is the
same rule as the 2026-10-03 built-in schedule resync
(`docs/decisions/agents/scheduling/2026-10-03-builtin-schedules-resync-from-repo.md`):
a copy derived from the repository converges to the repository, and a differing
copy is replaced and reported. The generated dashboard already follows it; its
render fallback and sidecar reconciliation are in
[[dashboard-provisioning.meiva.okf.md]].

Both outcomes are telemetry events, never only converge output: `converge_file_preserved`
was added after a frozen dashboard survived three consecutive rollouts (task #3689),
and `converge_file_overwritten` after the 2026-10-17 rename left the rendered
`delete-rules.yml` differing from its record — every converge preserved it, two
retired `checkpoint_blobs` rules kept evaluating, and nothing said so (task #5322).

Parent: [[cli/commands/docs/commands.meiva.okf.md]].
