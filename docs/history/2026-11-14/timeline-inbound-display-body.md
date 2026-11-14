# One display body for inbound timeline messages

Native timeline payloads retained model-facing envelopes while record timelines
reconstructed the original inbox body. A frontend first-paragraph heuristic
therefore erased ordinary user text. A stricter timestamp regex was rejected:
a person can paste a timestamp as their own first paragraph.

Normalize known writer framing once at the backend boundary, using its recorded
offset and source, and display/copy payloads without interpretation. Model input
and stored checkpoints remain unchanged. Historical missing or invalid offsets
preserve text rather than inventing a boundary. This intentionally favors a
visible old header over lost user content.

The existing upload protocol still reserves a standalone [files] companion
sentinel. Changing that protocol or adding provenance fields is outside this
repair. The Antigravity provider wrapper-matching risk is tracked separately;
it is not addressed by another display-layer parser.
