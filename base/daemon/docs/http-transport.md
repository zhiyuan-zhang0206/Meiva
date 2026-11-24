# Settings-free daemon HTTP transport

`base.daemon.http_transport.start_daemon_http` is the HTTP transport used by
`base.daemon.health.start_health_server`. It accepts an explicit address,
port, health response, route map and authentication token. It never resolves
configuration, creates a home, writes a PID or registers a unit.

Normal daemons retain their existing health wrapper: configuration-derived
port, the home its process entry hands in, process identity and aggregated
liveness payload. The wrapper resolves no home of its own, and the probes that
check an answering daemon's home take the one they expect as an argument. Unknown
routes return 404; configured bearer authentication precedes explicit route
execution; unauthenticated health responses contain no secrets. Parsing and
body limits remain shared rather than independently reimplemented. Agent-ops
supplies an OpenTelemetry tracer. For authorized routes, the transport extracts
W3C Trace Context and opens a server span around dispatch. Health probes stay
outside the traced routes.

A route answers bytes held in memory or a `FileBody`: an open file handle and the
number of bytes to send from its current position. The transport reads such a body
64 KiB at a time off the event loop and writes the header with the first chunk, so
the first byte leaves after one chunk is read and memory holds one chunk, not the
file. It owns the handle from the moment the route returns and closes it after the
send, including when the peer disconnects or stops reading for 30 s (the
connection is then aborted). A file that ends before its declared length closes the
connection rather than leaving it open for a peer to misread.

Connections are one-shot (`Connection: close`) unless the caller passes a
`KeepAlive`, as the agent-ops server does so that a client making many reads of one
host (a browser seeking through media, relayed by the gateway) pays one TCP
handshake and slow start instead of one per request. A reused connection serves
HTTP/1.1 requests in order, up to `max_requests` and `idle_seconds` of silence, and
is closed instead of reused when the client sent `Connection: close` or HTTP/1.0,
when the response left a declared request body unread (a 404 or 401 for a request
with a body), when the file body ended short, and when the server stops serving
(`server.close()` ends idle connections within a second). Framing a second parser
could read differently is refused with `400` and the connection closed, because on
a reused connection the leftover bytes would be parsed as the next request: any
`Transfer-Encoding`, a repeated or non-numeric `Content-Length`, and a line ending
other than CRLF. `/healthz` probers and every other caller keep `Connection: close`.
The SIGTERM drain of agent-ops covers route execution, not the streaming of a body
that follows it: a transfer still running at exit is cut and the client's
`Content-Length` check reports it.

Bounds, each with its reason. These are transport hygiene fixed in this module because it
never reads settings; the one limit that shapes what a user sees, the open-range slice, is
configuration (`settings.to_user`, see `base/config/docs/to-user-limits.md`).

- First request: header within 2 s, declared body within 10 s. They bound a peer that opens the
  port and sends nothing, or one byte at a time, so it cannot hold a handler task; real callers
  on the private network send a whole request at once.
- Stream chunk 64 KiB (`STREAM_CHUNK_BYTES`). Large enough that a multi-megabyte file costs tens
  of thread hand-offs rather than thousands, small enough that the first byte leaves after one
  cheap read and the write buffer (the transport's 64 KiB high-water mark) is filled at most once
  before `drain()` pushes back on a slow peer.
- Blocked write 30 s. A peer that accepts no bytes for this long while a response is pending is
  gone for practical purposes: a paused media element only closes its TCP window, and an
  open-range slice (2 MiB) fits the OS socket buffers, so a live client never blocks a write this
  long. The connection is aborted so a dead socket cannot pin its file handle and handler.
- Idle keep-alive 30 s (`KeepAlive.idle_seconds`). Longer than the five seconds `httpx` keeps an
  idle pooled connection, so a pooled client closes first and never sends on a connection the
  server is closing; short enough that abandoned connections do not accumulate.
- Requests per connection 100 (`KeepAlive.max_requests`). The server then asks the client to
  reconnect, so no connection (and whatever state its TCP path has accumulated) lives forever.
- Idle poll 1 s. An idle connection re-checks whether the server is still serving this often, so
  `server.close()` ends it within a second; asyncio's `wait_closed` waits for every open
  connection and a deploy would otherwise stall by the idle timeout.

The settings-free boundary supports explicit read-only tools without loading
ordinary daemon configuration. The transport does not validate a release,
prove process closure or grant startup permission; callers establish their own
admission before binding.

`base.native_process.evidence` owns strict digest/model values and the exact
native process identity. Service health imports them without loading rollout
leases. This evidence grants no startup or mutation authority.

The generic transport mounts only routes explicitly supplied by a caller.

The health wrapper requires the process entry's immutable `LoadedCommit`. Its
handler retains that exact image and reports `image.sha` without reading Git.
An explicit unknown stays unknown. The SHA enriches probe
version detail and does not determine readiness or authorize a restart.
