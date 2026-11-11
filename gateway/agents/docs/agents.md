# Gateway Agent API

`gateway/agents/` owns agent HTTP routes, request validation, durable acceptance
and response projections. Its `history/` package serves conversation and
timeline reads. Native lifecycle execution remains with the home runner and
agent host; an HTTP acceptance does not prove that execution completed.

Directory, launch receipts, history rendering and understanding requests retain narrow
readers from their gateway
lifespan's `ConfigBoot`. Window limits, retained depth, model defaults and clock
policy are read at the original operation boundaries; another lifespan's overlay
cannot alter them. A gateway profile without the agent domain reads understanding
switches freshly from its own authority's unit file, preserving the field defaults.

## Component owners

History reads fail visibly when checkpoint storage or delta reconstruction is
unavailable: `/timeline` and its `/conversation-snapshot` consumer return 503,
rather than an empty successful conversation. `/timeline/retained` starts at
the newest retained compact boundary, or an exact `checkpoint_id`; its
`before` cursor uses the existing historical item identity. It returns
`boundary_checkpoint_id`, `items` and `has_more`, without a live message count.
This is an explicit retained-history view, never a replacement for the agent's
live execution state. It does not read the live head, resume the agent, delete
history, or repair storage. Genuine empty agents and terminal pages remain
successful empty reads; failed retained reads return 503 and renderer bugs
propagate. Configured compact-history depth still bounds accessible segments.

- [Agent Router Surfaces](agents-router/agents-router.md) — lifecycle, list, state and per-agent read surfaces.
- [Guarded Agent Creation](guarded-creation.md) — keyed creation and original birth acceptance.
- [Guarded Draft Agent Creation](guarded-drafts.md) — keyed draft creation.
- [Cancel and Compact Requests](control-acceptance.md) — cancel and compact acceptance, separate from native application.
- [Guarded launch retry acceptance](launch-retry.md) — explicit native launch retry.
- [System Note Acceptance](system-note.md) — system-note writes.
- [Reply Suggestion](reply-suggestion/reply-suggestion.md) — gated, drafted reply shown in the empty composer.
- [Question Operation Receipts](operation-receipts.md) — keyed question operation receipts.
- [Completion notice digest](completion-notice-digest.md) — completion notice reads and digest ownership.
- [Agent Cross-Machine Transfer](agent-transfer.md) — cross-machine workspace and ownership transfer for quiescent native agents.
- [Guarded compound task assignment](../task_assignment/README.md) — compound task and agent acceptance with independent launch recovery.
