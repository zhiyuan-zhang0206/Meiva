# Error responses and middleware order

Unhandled errors preserve the typed 500 envelope and reflect credentials CORS headers only for the app's allowed request origins. Auth short-circuit responses also remain readable to an allowed browser origin.

Starlette places `ServerErrorMiddleware` outside user middleware and assigns the
catch-all exception handler to that outer layer. An unhandled 500 therefore
cannot rely on the inner `CORSMiddleware` to decorate its response. The catch-all
handler explicitly adds `cors_headers(request)`, using the same app-owned exact
origin allowlist; otherwise a browser reports an opaque network failure instead
of the actual typed error. A disallowed origin receives no such headers.
Within user middleware, CORS surrounds auth so an early authentication response
can still be read by an allowed browser client. The request-trace layer supplies
the error envelope's correlation identity.

Response compression (`gateway/http/middleware/compression.py`) is the outermost user
middleware: it gzips a JSON or text answer of 1 KiB or more for a client that accepts gzip,
after CORS, auth and the request-trace layer have decorated it, so an error envelope is
compressed like any other answer. Event streams, partial answers, answers that already carry
a `Content-Encoding`, and opaque payloads (media, archives, PDFs, workspace files read by byte
range) are left alone. Level 5 and the 1 KiB floor are the same trade as the runner's transport
(`base/daemon/docs/http-transport.md`): a 100 KB timeline window is about 26 KB after 1.5 ms, and
Starlette moves a body of 128 KiB or more to a worker thread so the single event loop does not wait.
Clients that send no `Accept-Encoding` (the SDK's urllib calls) keep the plain body.

The Gateway's ASGI entry verifies the active generation's machine API bearer
before allowing OpenTelemetry to extract an incoming trace. Requests with
human credentials, browser sessions or no machine credential get a new trace
using local sampling. The boundary clears the configured propagator's headers
before any trace extraction for those requests, including authentication
rejections.

Task-assignment authentication tests use fixed isolated test credentials, including a deliberately rotated credential. A previously accepted request cannot replay under the revoked credential, and the denied replay creates no additional business rows. These literals are fixture input, not deployed secrets.

## Relationship to Other Nodes

- [Gateway](gateway.md) — the domain node whose error surface this describes.
