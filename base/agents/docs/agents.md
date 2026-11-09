# Agent contracts and evidence

`base/agents/` owns cross-process agent identity, messages, observation and history.
Its component nodes describe the current contracts and implementation.
`recovery/` groups shared page-recovery notices and durable automatic-recovery gates.

## Documented components

- [Agent harness identity](harness/harness.md) — Immutable harness identity and its local availability probe
  ([launchable set and the birth gate](harness/launchable-and-birth.md),
  [which models a harness runs](harness/harness-models.md)).
- [Hierarchical Understanding](../history/hierarchy/docs/hierarchy.md) — Hierarchical Understanding.
- [Per-message true tokens](../history/docs/message-tokens.md) — Per-message true tokens.
- [Agent inbox](../inbox/README.md) — Agent inbox.
- [External Caller Profiles](../messages/docs/external_caller.md) — External Caller Profiles.
- [Inbound Provenance Facts](../messages/docs/inbound-provenance.md) — Inbound Provenance Facts.
- [Message Metadata Contract](../messages/docs/kwargs.md) — Message Metadata Contract.
- [Agent observation evidence](../observation/README.md) — Agent observation evidence.
- [SDK-call sampling](../sdk/docs/sampling.md) — Live SDK-call sampling and configuration admission.

- [Immutable upload storage and native identity](../upload_delivery/docs/storage.md) — Immutable native upload storage.
