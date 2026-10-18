---
type: doc
title: scripts/content_lint/docs/panel-jargon — User-Facing Panel Gate
description: The lint_panel_jargon.py gate — body findings, the configurable rule file, budgets and exit status for a workspace to_user.md panel.
tags:
- scripts
- lint
---

# scripts/content_lint/docs/panel-jargon — User-Facing Panel Gate

A panel such as a workspace `to_user.md` is read by the user, who has no context
about internal identifiers: an explanation that is accurate but written in
codenames does not let the reader catch up. The gate splits the document at the
first evidence heading (identifiers, paths and links belong there) and reports
body findings for a rule set that lives entirely in
`panel_jargon_rules.toml`: bare change/task numbers and issue numbers are hard
rules, while paths, batch codewords, unglossed acronyms and the glossary length
are counted against the configured budget. It never edits a panel.

The body of a panel — everything before the first evidence heading — carries no bare internal identifier, issue number or batch codeword: those are hard findings that exit non-zero. Soft findings (repository paths, unglossed acronyms) and the number of glossary entries are bounded by the same rule file, and exceeding either budget also exits non-zero. Identifiers inside the evidence area are never reported, the rule file is the only place patterns and budgets live, and the gate is read-only.


## Known vocabulary stays as written

The gate reports internal identifiers, not English. Terms the reader already uses
(`agent`, `watcher`, `skill`, `MCP`, `task`, `PR`, `CI`, `checkpoint`, `rollout`,
`gateway`, `runner`, `context`, `prompt`, `token`, `session`, `sandbox`,
`subagent`, `self-terminate`, `Trunk`, `Grafana`) keep their original form: a panel
explains what the thing does on first use and afterwards uses the same word. The
`keep_terms` list records that vocabulary, and `allowed_acronyms` adds the
uppercase forms (`MCP`, `LLM`, `CLI`, `SQL`, `YAML`, ...) so the acronym rule never
pushes a writer toward a translation.

The one soft rule in this direction, `invented-translation`, is deliberately
narrow: it lists the few Chinese replacements seen in review (`watcher` written as
`\u503c\u5b88\u8fdb\u7a0b`, `self-terminate` as `\u81ea\u6211\u7ec8\u7ed3`, ...) and
names the original term to restore. It is not a translation dictionary, and
extending it beyond observed cases would recreate the over-correction it exists
to prevent.

## A section label must be recognised, not guessed

A heading is a Markdown heading line, or a line whose entire text is bold, with an
optional trailing colon. Everything after a heading until the next one belongs to
that section, so an unrecognised label does not just lose its own line: the
glossary stays counted as body and its entry cap becomes a no-op, and identifiers
in a mislabelled evidence area are reported as body findings.

The gate therefore reports `unparsed-section` when a line carries a glossary or
evidence label that is not a heading. It is soft, because the writer may have
meant prose, but it names the label: a panel that looks like it passed without its
glossary ever being read is the one failure mode a reader cannot see.

Glossary terms are counted from the bold lead of an entry (`**term**: ...`), a
Markdown table row, or a bullet whose term is in backticks (`- `term`: ...`), so the
entry cap does not depend on which of the three forms a writer picked.


Parent: [[scripts/content_lint/docs/content_lint.meiva.okf.md]].
