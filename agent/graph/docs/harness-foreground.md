# Foreground input and cancellation

An external claim prepares the initial input and keeps its chat rows visible in
the pending strip. The timeline and inbox are committed together after the
executor accepts the input. Codex reports acceptance from its correlated start
response. For adapters without a start receipt, the first attributable
activity or terminal result is evidence that the input reached execution.

While an adapter that implements `ForegroundSteering` (Codex, Claude Code,
Antigravity) is active, all pending steerable inbounds at the head of the inbox
— chats from any source (including attachments and peer-agent reports), system
notes, and heartbeats — are reserved together as one FIFO batch and steered
into the running foreground in a single request as long as no `cancel`,
`restart`, or `terminate` control is pending. Steered rows stay pending with a
reservation timestamp until the provider confirms receipt, at which point their
timeline items and inbox consumption commit together. A definite rejection
releases the batch reservation for the next claim. An unconfirmed delivery
leaves uncertainty markers and is not automatically sent a second time.
Adapters without `ForegroundSteering` leave queued work for the next claim.

Cancel targets the foreground. The graph continues reading activity until the
executor reports the terminal outcome. A normal finish that races a cancel
remains a normal finish. The cancel request does not retire a healthy session
or stop independent background work. A provider that lacks scoped cancellation
reports it as unavailable and the foreground continues. Restart and terminate
remain separate lifecycle commands and can still interrupt a graph invocation.

Parent: [Harness turn](harness-turn.md).
