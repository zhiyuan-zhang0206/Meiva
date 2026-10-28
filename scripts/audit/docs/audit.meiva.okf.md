---
type: doc
title: "Audit scripts"
description: "scripts/audit/ — read-only audits of state that lives outside the tree: GitHub branch protection, reference and move inventories, runtime impact, SDK usage and a live database's schema."
tags: [scripts, audit]
---

# Audit scripts

Read-only tools that make an external or derived state observable: GitHub branch
protection stored outside Git, the references a move must not leave behind, the
runtime-impact graph's own numbers, the SDK surface in use, and a live
database's columns against the `db/schema.sql` baseline. None of them writes to
the repository, the database or the remote.

`branch_protection.py` compares live GitHub protection with `.trunk/trunk.yaml`.
Before changes, `where_used.py` groups importers, tests, strings, docs and baseline
entries of symbols/modules/paths (`--json`).
After moves, `module_moves.py` rejects remaining old references and missing names.
Both CLI owners expose `main(argv, *, repo_root=None)` with shared diagnostics and
exit codes, defaulting to their checkout. Move imports use the caller's Python
environment; validation algorithms stay local.

Each of these tools is an effect shell — it reads the checkout, the GitHub API or the
database and prints — and `analysis/` owns what it decides: `references.py` finds the
references behind `where_used.py`, `report.py` groups and renders them, `moves.py` answers
what a move leaves behind, `protection.py` states the branch-protection contract,
`schema.py` diffs the DDL against the live columns, `reach.py` measures the impact graph,
and `frozen.py` names the history axes neither audit rewrites.

`schema_drift.py` diffs a live database's public columns against `db/schema.sql`
and classifies every difference:
[[scripts/audit/docs/schema-drift.meiva.okf.md]].

Parent: [[scripts/docs/scripts.meiva.okf.md]].
