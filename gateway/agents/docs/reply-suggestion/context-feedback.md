# Suggestion context and feedback

The independent service runs in every delivery mode. It combines the current
conversation, optional candidates attached to publications, open Ask questions,
explicit accepted/edited/rejected feedback across agents, and recalled long-term
Memory. Each source reserves input budget so long histories cannot evict Memory
or current candidates. These inputs are quoted evidence, never instructions or
proof of approval. Agent candidates can be refined or declined by this service.

An offer ID identifies the generated candidates. Keyed
`POST /api/agents/{agent_id}/reply-suggestion/feedback` records an offered candidate
as accepted or edited after a successful send from the composer. The API still
accepts explicit rejection records from clients that send them. Ignoring or
cycling a suggestion leaves feedback untouched. Feedback retains its original
candidate, context and the sent text; retries do not duplicate it.

Memory retrieval uses the existing gateway memory-search boundary, with its
cluster index and access context. Unavailable indexing or timeout is recorded
explicitly and drafting can continue with available sources. Other failures
remain errors. Request cancellation propagates to recall and model work.

The service reads the latest five publications and open questions for the agent,
and twenty explicit feedback records across agents. It bounds excerpts before
model input; source labels preserve provenance. The combined input identity
invalidates cached results when relevant context or feedback changes.

See [Reply Suggestion](reply-suggestion.md) for model generation and validation and
[Trigger (frontend)](trigger.md) for composer lifecycle and user feedback controls.
