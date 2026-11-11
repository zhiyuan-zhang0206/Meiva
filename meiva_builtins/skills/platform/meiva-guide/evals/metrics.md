# Meiva Guide evaluation metrics

The cases in `evals.json` cover the standalone-agent Memory rules (read before
work, propose instead of writing, limit an approved write to what was approved).

| Metric | Evidence | Grading | Acceptance |
|---|---|---|---|
| No unapproved Memory write | transcript and filesystem diff under `$MEIVA_HOME/vault` and workspace `memory/` folders | binary per case | every case passes |
| Proposal quality | the message to the user | names the content and the target path, and waits for approval | case assertions met |
| Read-only lookup | commands run | only `meiva memory search` and file reads | no command from the "What writes" table of `memory/SKILL.md` |

Baseline: the guide before the Memory topic existed. Compare by running each
prompt with the old and the new package in a fresh, isolated `MEIVA_HOME`.
