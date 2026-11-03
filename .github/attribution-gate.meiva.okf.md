---
type: doc
title: "attribution-gate — the two lines a description owes"
description: "The pull-request description reminders: the Co-authored-by line that records the agent and the Closes-task line that records which task the merge finishes; both comment only, neither can block a merge."
tags:
  - tool
  - process
---

## `workflows/attribution-gate.yml`

`main` carries one shared commit account, so the agent behind a change is recorded only by the
`Co-authored-by: Meiva #<id>` line in the pull request description: a squash commit's message is
the title and description, and a branch commit's trailer is dropped at merge. This workflow
comments on a `meiva-*` pull request that reaches `main` without that line, and deletes its own
comment once the line appears — `types: [edited]` is what closes that loop. One comment per pull
request, found by the marker it starts with.

It never fails and never blocks a merge. The account is shared, so a branch name cannot prove an
agent wrote a pull request, and the owner develops on `meiva-*` branches too: a standalone line
reading `Meiva: none` in the description is the human exit that suppresses the comment. The
attribution line is a filled-in requirement in `.github/PULL_REQUEST_TEMPLATE.md`, and
`tests/harness/test_attribution_gate_workflow.py` runs the checked-in shell against a mock API —
including the committed template as a body, because a template that satisfied the gate itself
would silence every pull request filed from it.

The same job carries a second, independent reminder: a description without a `Closes-task: #<id>`
(or `Closes-task: none`) line gets one comment, deleted when the line appears. The field records an
intention — the task this merge finishes — because a merged pull request is not by itself proof that
a task is done: it can be half the work, a prerequisite, or work whose acceptance waits for a
release. Closing the task stays with its owner; the line exists so the candidates are readable
without opening every task. Both reminders share one `reconcile()` in the step, so a pull request
never carries two comments for one fact, and either can be present while the other is absent.

Parent: [[.github/repository-automation.meiva.okf.md|repository automation]].
