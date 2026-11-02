---
type: doc
title: "meizel/ — What one recorded entry says"
description: "The count, owner and until a [debt] line carries, the two shapes an until takes, the marked placeholder debt record writes, and the three numbers the summary counts apart."
tags:
  - locality
  - tool
---

# meizel/ — What one recorded entry says

A `[debt.<code>]` line records a count, the task that re-reads it, and the condition it waits for:

```toml
[debt.ambient-read]
"boot.py#environ os.environ" = { count = 1, owner = "#7139", until = "waits for the Settings port: boot.py::build reads os.environ at import and the port it would take does not exist yet" }
```

`count` is required and positive, and `owner` and `until` are required with it: a line that leaves
either out is `bare-debt`, one diagnostic per entry at the entry's own line, naming the fields it
lacks. An `until` also has to begin with `waits for ` or `permanently:` — the two shapes say different
things, and a sentence in neither shape reads as an answer without saying which question it answers —
or with `unreviewed:`, the marker below. The full text of what `until` and `owner` each answer is printed once per run, on the first
entry that lacks that field, so a baseline with twenty bare entries prints twenty lines rather than
twenty copies of the rule. An empty or unknown field is `manifest-debt` rather than dropped — a
misspelt `owner` must not read as "no owner". `record` and `prune` keep the metadata while they
change the count.

An entry `record` writes without being told what the debt waits for carries a marked placeholder
instead of an invented reason: `until = "unreviewed: recorded by <who> on <date>"`. The prefix is how
the summary counts it, and it is meant to be read and replaced — a mechanically written sentence that
looked like a reason would be worse than no sentence, because the next reader would stop there.

The account never carries what a rule allows: a `[roots]` kind a `[debt.ambient-read]` entry still
charges for is `legal-read-recorded` — `check --changed` sees it too.

`check` reports every entry that leaves out either field, and `debt` counts three states apart, each
with the occurrences it carries:

- `N recorded debts name no owner`;
- `M recorded debts state no exit condition (no `until`)`;
- `K recorded debts carry an unreviewed exit condition`.

All three are read straight from the `[debt]` tables, not from the diagnostics, so a reader can ask one
without borrowing another's answer, and no state masks another: an entry with a placeholder is not
counted as missing an `until`, and an entry with neither is counted in both. The first two are gates —
an entry that names nobody, or waits for nothing, fails `check` and CI. The third is a review queue:
`record` writes it, only a person clears it, and the number can only fall when someone reads an entry
and writes down what it actually waits for. `debt --unowned` lists the ownerless entries as
`<manifest>: <code> <key> (<count>)`. An entry that is not in your lane belongs to the task its
`owner` names — send that task a message rather than editing it.

Parent: [[meizel/docs/checker/debt/debt.meiva.okf.md|recorded locality debt]].
