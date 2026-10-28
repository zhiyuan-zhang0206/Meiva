---
type: doc
title: "meizel/ — The two measures of a debt diff"
description: "What a debt diff counts as the measuring device (rule code, policy and manifest declarations) and what it counts as content, how the reference is regenerated under the change's device, and how the rules and content columns are judged."
tags:
  - locality
  - tool
---

# meizel/ — The two measures of a debt diff

A change moves a recorded number in two ways: the code got better, or the device that measures it
changed. `debt diff` reports them apart, because a declaration that removes a read from the record is
not a repair and must not look like one.

- `debt diff --against <ref> --on-growth fail|warn` compares the recorded
  totals per code with those at a git ref. `--by-target` adds the recorded patch
  debt per target group: what the change paid down or added, to paste into a PR
  description. Entries are counted by the target in their key, so debt that
  moves with its file stays in its group. The groups are the first `--depth`
  dotted names of a target (default 3), or the `--target-prefix` values (a target
  counts under the longest one, the rest under `(other)`; a listed prefix always
  shows). Totals, not directories, are compared, so moved debt is not growth.
  The caller chooses `fail` or `warn`; there is no default.
- A change that edits **what is measured** is judged in two parts
  (`meizel/contracts/debt_rules.py`): `debt diff` detects it by itself and prints `rules −N` and
  `content −M` on their own line, plus one line per declaration, policy entry or rule file it
  edits. The measuring device is the checker's rule code, `meizel.policy.toml`, and the
  **declarations** of every `meizel.toml` — everything but its `[debt]` tables: `exports`, `uses`,
  `loads`, `external`, `roles`, `purity`, `state`, `roots`. A change that edits only `[debt]` tables
  is not a rule change and keeps the totals-only report.
- **rules** is what this checker, run on a checkout of the ref, records for the ref's unchanged
  code, minus what the ref records. Regenerating the ref writes the change's manifests, minus their
  debt tables, over the ref's content, so declaring a root, a legal edge, an approval or a purity
  mode moves the number into this column: it is reported, never judged, and it is never repayment.
  **content** is what the change records beyond that: judged. Every recorded key must be one the
  rules find in the ref, with no greater count, so a declaration cannot hide a new violation and a
  violation cannot ride along. The reference's content is measured at the change's own paths — a file
  git reports as renamed is moved before it is measured — so a rename is neither a rule change nor
  growth, for file keys and edge keys alike, and a declaration that follows the rename lines up. Only
  the rule code and the policy are the change's own in that replay: every other file under `meizel/`
  (tests, the state rules, `allowances.py`) is content and is measured as the reference left it. A
  manifest the checker cannot read in the ref stops the comparison. Historical snapshots keep the
  unreadable gate. See [[meizel/docs/checker/policy.meiva.okf.md|historical policy comparisons]].
- Deleting a whole `[uses]` or `[exports]` entry is **not** a rule change: the reference's replay
  declares the legal edges it finds again, so the debt it uncovers is recorded as **content** growth.
  Adding an entry, or narrowing one, is the direction that splits.
- `--waive-if-changed <pathspec>...` adds pathspecs that also count as a rule change (CI passes the
  rule-code set). It only adds to the detection above; a change that touches none of them is judged
  by totals, with no waiver.

Parent: [[meizel/docs/checker/debt/debt.meiva.okf.md|recorded locality debt]].
