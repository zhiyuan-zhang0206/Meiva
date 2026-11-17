# Durable terminal fence state

The first lost-ack correction kept an agent's terminal cutoff only in the PTY
service process. That closed requests already queued in that process, but a
client or proxy can hold a valid old `new` request until after the service
restarts. The replacement would then have no cutoff and could allocate the
pre-terminal index after resurrection.

Terminal cutoffs now live in the owner-only atomic file
`$MEIVA_HOME/run/pty-terminal-fences.json`. The lifecycle backend records the
maximum cutoff before it asks the service to close PTYs. The service restores
that state before admitting new agent shells and records it again while serving
the fence operation. A missing or refused socket is therefore an unknown close:
the lifecycle path keeps its TTL rows for the next retry, while a later daemon
will still reject the old numeric request. Corrupt fence state fails closed for
agent-shell allocation rather than treating an unreadable file as no terminal
transition.

The regression sends `new`, loses the client acknowledgement, records and
acknowledges the fence, restarts the PTY daemon, then releases the old request.
It must be refused, while the next higher session index remains allocatable.

Update (2026-11-17): workspace export records this durable cutoff before it
dials `pty-sessions`. If that service is unavailable, transfer may still package
the workspace with no confirmed killed-session ids; the cutoff protects the
next daemon generation. The exception is limited to `ServiceDownError`, so an
invalid fence state or any other cleanup failure still aborts the export.

Correction (2026-11-17): incremental review rejected that export fallback. A
durable cutoff constrains the next daemon generation, but a payload created by
the unreachable generation can still be alive and mutating the source workspace.
Workspace export therefore requires a successful fence acknowledgement and
fails closed on `ServiceDownError`; only ordinary termination may defer cleanup
while retaining TTL rows for later convergence.
