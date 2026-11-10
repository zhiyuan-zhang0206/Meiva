# Harness session identity

Part of [Provider harness](harness.md).

`FileSessionRegistry` writes `agent_id ↔ session_id` (plus cwd, model, resume
count and the harness that wrote it) as soon as the adapter reports the session
started. An agent keeps one record per harness it has used
(`<agent_id>.<harness>.json`): switching harness never removes a record, so
switching back resumes the conversation that harness had, exactly as any other
resume does. Writes are a temp file
plus `Path.replace`, so a reader never sees a half-written record, and a corrupt
document raises instead of silently starting a fresh session under an id that
already has history.

A registration is not a second identity: it is the bridge's claim that a session on
this machine belongs to one agent. Three facts gate it — the row says `attached`,
`expires_at` has not passed, and the calling process descends from
`controller_pid` — and the expiry is the one the bridge cannot forget, because the
others degrade silently: a pid gets reused, and a row that never expires would be a
bearer capability nobody can revoke. The bridge refreshes the row when a turn
starts and every third of its TTL while the turn is running, including while the
provider is quiet. A retired session removes the row; an abandoned row expires
after its last renewal.

`SessionRecord.harness` is required and names the document the record lives in:
an adapter loads and saves only its own harness's record (`BaseHarnessSession.harness`),
so one provider's transcript is never resumed under another provider's session.

Records written before one-record-per-harness lived in `<agent_id>.json`. Host
converge (`harness session records layout`, right after the home skeleton) migrates
them once: each file is written to its harness's document, then removed; a record
with no harness field is named `meiva`, the only harness that predates the field;
a file that is not a valid record fails the step naming the file. The registry has
no read path for the old layout: while such a file remains, a load refuses and says
to run `meiva converge`. The migration prints how many records it moved; a converged
home has no `<agent_id>.json` files and prints nothing.
