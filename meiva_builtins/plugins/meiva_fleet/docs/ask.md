# Ask contract

`meiva.ui.ask(questions)` creates durable questions in the composer's pending-question queue and
returns one question ID per item, in the order given. `questions` is a non-empty list; each item is
`{"title", "content"?, "type"?, "options"?, "note_suggestion"?}`. The title names the decision and
content supplies context. `type` is `text` (the default, one answer field), `single_select` or
`multi_select` (ordered `options`, each with an optional Note). `note_suggestion` offers candidate
text for the answer or Note until the user explicitly applies it. An invalid item rejects the whole
call; one call is one idempotent operation (the gateway replays its accepted IDs and refuses changed
content under the same identity). Each question is its own `agent_questions` row with its own ID;
nothing records which call created it.

An agent can have any number of open questions. They do not expire and never overwrite each other.
The runtime does not end the turn when questions are asked. `meiva.ui.close_question(id)` closes one
of the agent's own unanswered questions (`resolution='withdrawn'`); a question cannot be edited.

The user answers all of an agent's open questions in one reply: each is answered or skipped, in
question ID order. The reply is one inbound to the agent with a `Re: question #<id> "<title>"` block
per question; a skipped question reads `Skipped by the user.` and grants nothing. Reading a
publication does not answer any question.

The user gets one system notification per agent: the first open question alerts, later ones update
it silently ("N questions awaiting an answer"), and it is retracted when the agent has no open
question left.

A pending question appears in its agent's question bar above the composer and as a question mark on
the agent's sidebar row ([decision](../../../../docs/decisions/frontend/ui/2026-11-11-remove-inbox-and-counts.md)).
See [meiva.ui](../../../../meiva/docs/ui.md) for the mode and publication contract and
[Numbered questions](questions.md) for the answer types.
