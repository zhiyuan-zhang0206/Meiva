# No-silent-failures lint

`scripts/lint/diagnostics/no_silent_failures.py` (pre-commit `lint-no-silent-failures`) flags
production code that turns a failure into "nothing happened": a
`contextlib.suppress(...)` naming `Exception` / `BaseException`, or an `except`
for `Exception` / `BaseException` / bare whose body neither raises, nor logs at
WARNING or above, nor emits a telemetry event, nor writes to stderr, nor hands
the bound exception on (a debug or info line does not count: below every sink's
threshold). It exists because a `suppress(Exception)` hid an after_exec hook
that raised on every call from the day it shipped.

Fix order: narrow to the expected exception type, delete the handler, or keep it
and report with the traceback (`logger.opt(exception=True).warning(...)`).
Opt-out `# silent-ok: <reason>` on the handler line, reserved for a reporting
channel's own failure path; there is no allowlist file. Scope
`meizel.roles.framework_dirs` + `scripts/`, `schedules/`, `.agents/`; tests are not
scanned. The authoritative rule text is the script's module docstring.

Parent: [lint](lint.md).
