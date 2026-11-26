# Quote admission

Chat and keyed upload admission validate a quote against a stored source in the
destination agent's current harness epoch. Both routes persist the selected text
as user-supplied context. The selected text and author are supplied by the client,
so the lookup establishes source membership and item identity. It does not
compare browser-rendered text with the stored Markdown.

Native quotes with a stable message or inbox ID and block number resolve against
committed checkpoint messages, including retained compact boundaries. The native
block coordinates come from the same source parser the renderer uses. The
timeline cursor and its configured history depth do not select the source.

Record quotes carry a session-qualified identity in the existing
`source_message_id` field. The agent host uses the adapter's entry key at the exact
complete record offset, or the inbox row ID and its recorded input key. The same
identity is emitted in live and cold timeline items. Admission consumes an exact
source receipt whose activity state is checked against the agent's current status.
Display paging, rendering policy and intra-row display order have no role in
resolving a canonical quote.

Requests with only legacy cursor coordinates still use the original path and
respect its existing history view and anchor checks. Old runners can continue
serving that path during a rolling upgrade. A canonical request on an older or
unreachable runner returns 503 and can be retried on a compatible runner. A
different agent or session, an absent entry or block, and a still-running source
return 422. The HTTP quote schema and the persisted snapshot shape are shared
across versions.
