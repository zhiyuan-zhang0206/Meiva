# meiva:note
# The lines `meiva status` prints about this cluster's data plane, as values.
#
# The shell probes Postgres, Redis and the pooler and hands each reading to the function that
# owns its wording. A probe that failed to answer reports `✗`; nothing here invents a `✓`.

from __future__ import annotations

__all__ = [
    "failure_detail",
    "local_pg_line",
    "local_redis_line",
    "pooler_line",
    "probe_line",
    "remote_status_header",
]


# meiva:note
# One probe line: the mark is the reading, the text is the probe's own detail.
def probe_line(*, ok: bool, detail: str) -> str:
    return f"  {'✓' if ok else '✗'} {detail}"


# meiva:note
# A remote-managed plane has no local instance to manage; its URLs are probed instead.
def remote_status_header() -> str:
    return "  · data plane remote-managed — probing the URLs (no local instance)"


# meiva:note
# The local Postgres line.
#
# `pg_isready` alone answers without authenticating, so a server that is up but refuses the
# delivered gateway login reports `reachable but connect failed` rather than a false `✓`:
# `connect_error` is the last line of the failed pooled `SELECT 1`, `None` when it succeeded.
def local_pg_line(*, host: str, port: int, running: bool, connect_error: str | None) -> str:
    where = f"{host}:{port}"
    if not running:
        return f"  ✗ postgres ({where}) unreachable"
    if connect_error is not None:
        return f"  ✗ postgres ({where}) reachable but connect failed: {connect_error}"
    return f"  ✓ postgres ({where})"


# meiva:note
# The local Redis line.
def local_redis_line(*, host: str, port: int, ok: bool) -> str:
    return probe_line(ok=ok, detail=f"redis ({host}:{port})")


# meiva:note
# The pooler line.
#
# Its port is a registry fact only: with no record the port is unknowable, which is said
# instead of printing a false `:0`. `refusal` is the authority store's reason when the operator
# entry could not be read; otherwise `ok` is the admin-console probe's reading.
def pooler_line(*, port: int | None, ok: bool, refusal: str | None) -> str:
    if port is None:
        return "  - pgbouncer: no registry record — cannot resolve its port"
    if refusal is not None:
        return f"  ✗ pgbouncer (127.0.0.1:{port}): {refusal}"
    return probe_line(ok=ok, detail=f"pgbouncer (127.0.0.1:{port}, transaction pooling)")


# meiva:note
# The last non-empty line of an error's text: the detail that names what went wrong.
def failure_detail(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else "connection failed"
