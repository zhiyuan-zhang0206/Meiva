---
type: doc
title: Ask — durable questions requiring a user response
description: Core SDK Ask replaces agent notifications while retaining question and answer history.
tags: [fleet, user-communication]
---

# Ask contract

`meiva.ui.ask(title, content=None, *, question=None, note_suggestion=None)` creates a durable question in Timeline and the user's Ask queue.
The title names the decision and content supplies context. Omitted `question`
means text; choice questions use `single_select` or `multi_select`. Every type
supports an editable Note. The agent's `note_suggestion` is a draft, never an answer.

An agent can have several open questions. New questions have no expiration and
never overwrite an unanswered question. The runtime preserves operation identity across retries; the gateway replays
its accepted ID and refuses changed content under that identity. The runtime does not end the turn
when a question is posted. Use `meiva.ui.questions()` to inspect pending questions.

Answers and Notes commit with one agent inbound. Closing without answering
records a dismissal and grants no authorization. Reading a publication does not
answer any question. New Ask records create no notification delivery event.

The old agent-facing notify/edit/dismiss SDK entries are removed. Existing
`agent_notices` IDs, canonical answers and keyed answer receipts remain their
storage owner. Pending legacy questions retain their IDs and lose automatic
expiry; completed history remains intact. Legacy internal writers and system
alerts keep their existing protocols. See [[meiva/docs/ui.meiva.okf.md]] for the
mode and publication contract, and [[notice-questions.meiva.okf.md]] for answers.
