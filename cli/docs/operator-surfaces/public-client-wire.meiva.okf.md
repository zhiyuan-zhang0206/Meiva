---
type: doc
title: Public CLI client wire boundary
description: Real client HTTP, parser, provenance and schedule verification contracts.
---

# Public CLI client wire boundary

Agent and schedule client tests live together in `cli/tests/wire`, above their
command and parser consumers. This keeps the client test boundary from reversing
production dependencies. They use a scoped loopback HTTP listener and fixed
source-visible command children with private homes. The children select the
actual gateway profile before reading configuration, use the canonical gateway
address and authorization owners, and invoke public command/parser entries.
They do not start an agent, supervisor or live gateway application. The listener
records method, path, query, headers, raw body and decoded JSON; an unknown route
or method is a parent-process failure, including when the command expected an
HTTP error. Server shutdown, serving-future observation, close and thread joins
remain lexical even when a child or assertion fails. The existing bounded
process runner owns each command child and its descendants.

Directory pagination and name-to-id resolution retain their exact request
arguments and ordering. Partial schedule updates omit unspecified fields;
enable/disable remains tri-state. Script-source XOR, unreadable files, empty
updates, unknown names and declined deletion preserve zero mutating requests.
Actual stdin supplies heredoc scripts and confirmation answers. A no-body
restart, resurrection or control request is distinct from JSON null and an
empty object. Error bodies are rendered before an unexpected status propagates;
HTTPStatusError, ProvenanceError and unknown-result ValueError checks execute at
the real call inside the child, with HTTP status and request path checked too.
A result receipt distinguishes an expected return/error assertion from a child
that failed to boot; it does not manufacture exception objects in the parent.

Agent list rendering preserves stable columns, the continuation cursor and
omission of runner-local workspace paths. Send retains its explicit source,
bounded last-three-line/2048-byte tail, unreadable-tail delivery and empty-tail
behavior. Terminate and kill retain their force and shell-session flags,
asynchronous acceptance and present/missing shell-kill reports. Explicit source
provenance is unchanged with a real external-caller environment present or
absent; missing or invalid source never becomes an HTTP write. Parse errors and
programmatic command failures keep their distinct exit/error contracts.

Schedule verify keeps its existing explicit sweep ports. Rows-file mode still
uses the actual file reader and dry-import checker, suppresses notification,
and never enters the explicitly supplied producer. Its test records producer
entries before returning the real capture pipeline and asserts both zero entries
and no verification event: telemetry failure isolation cannot hide an accidental
producer call. Real report/event, provision/database and foreign-checkout tests
remain separate from these client HTTP observations.

The schedule sweep retains blank scripts as green, one RED line for each failed
script, and RESULT counts on a clean pass without a failure report. A read error
remains rc 2 with TOOL-ERROR and a report. Check-file runs the actual dry-import
child: unresolved modules and syntax errors are RED, and clean imports are
CHECK-OK. The emitted error-level verification event retains its count fields
and failing-script details. Gateway-backed verification includes stopped rows,
reads each script from its per-id route and returns rows in id order. Built-in
provisioning writes the actual database, enables product schedules and remains
idempotent on a second run.

Script-source XOR is checked before opening either file, including the
both-sources case; gateway compilation details with their line number reach
stderr. Numeric schedule identifiers avoid a list round trip. The agent card
fixture contains every field the renderer reads, and an unreadable tail is
still delivered as an inline unavailable notice. Whitespace-only tails add
nothing. A requested shell kill without a report is visible for an older runner;
no shell-kill request stays silent.
