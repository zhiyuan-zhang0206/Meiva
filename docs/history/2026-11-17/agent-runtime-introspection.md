# Agent runtime introspection

Agents need to inspect their effective runtime selection after a restart, harness
switch or transfer. The existing detail read and startup identity own that answer;
a separate SDK method would duplicate the CLI read surface. The detail snapshot
uses the same model resolution as roster cards and preserves the stored override
map separately from the effective selection.

Lifecycle scheduling is owned by the companion implementation slice. The guides
explain its one-shot quiescence contract and cancellation so agents can coordinate
self and peer operations without a second lifecycle state machine.
