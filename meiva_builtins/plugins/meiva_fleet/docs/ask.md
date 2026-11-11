# Ask contract

`meiva.ui.ask(title, content=None, *, question=None, note_suggestion=None)` creates a durable question in the composer's pending-question queue. Resolved questions stay in timeline history.
The title names the decision and content supplies context. Omitted `question`
means text with one answer field; choice questions use `single_select` or
`multi_select` and keep an optional Note. `note_suggestion` offers candidate
text for the answer or Note until the user explicitly applies it.

An agent can have several open questions. New questions have no expiration and
never overwrite an unanswered question. The runtime preserves operation identity across retries; the gateway replays
its accepted ID and refuses changed content under that identity. The runtime does not end the turn
when a question is posted. `ask` returns the question's ID; `meiva.ui.close_question(id)` closes
one of the agent's own unanswered questions (`resolution='withdrawn'`), and a question cannot be edited.

Answers and Notes commit with one agent inbound.
The user closing without answering records a dismissal and grants no authorization.
Reading a publication does not answer any question. Each Ask creates a system
notification that is retracted when the question is answered, dismissed or closed by the agent.

The old agent-facing notify/edit/dismiss SDK entries and the Inbox page are removed.
`agent_questions` rows, canonical answers and keyed answer receipts remain the storage owner
of questions, and a question is the only kind of open item: a migration resolved every
still-open row that was not a question, or that carried an expiry, as `expired`. A pending
question appears in its agent's question bar above the composer and as a question mark on
the agent's sidebar row ([decision](../../../../docs/decisions/frontend/ui/2026-11-11-remove-inbox-and-counts.md)). See [meiva.ui — User Interface](../../../../meiva/docs/ui.md) for the
mode and publication contract, and [Numbered Question Questions](questions.md) for answers.
