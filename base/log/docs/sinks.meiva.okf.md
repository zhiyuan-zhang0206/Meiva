---
type: doc
title: Log sinks
description: 'The three loguru sink types the `base/log/process.py` init entry points assemble — stderr, the JSONL file and the unified event pipeline — how every sink is registered with `diagnose` off, and the stdlib interception that routes `logging.getLogger` records into them.'
tags:
- base
- library
- observability
---

# Log sinks

The sinks [[log.meiva.okf.md|logging]]'s `init_*` entry points assemble per
process type. `base/log/sinks.py` owns the file-sink mechanics and the stdlib
interception; the event-pipeline sink stays in `base/log/process.py`.

## Three sink types

- **stderr**: human-readable colored format `_HUMAN_FORMAT` (`time level a=agent_id message`), aligned with terminal scrollback habits — **not logfmt**.
- **File**: JSONL (`serialize=True` serializes the entire record as a single JSON line), rotated at 100MB / kept for 7 days (`_add_file_sink`). Without rotation `gateway.log` once grew to ~900MB.
- **Unified event pipeline**: `_event_sink` derives each INFO+ record to an event and enqueues it into `base/telemetry` (bounded queue + drain thread). The emitter writes each batch to the JSONL mirror + OTLP export (unified schema: `ts / trace_id / span_id / agent_id / machine / cluster / process / category / event_name / level / source / target_agent_id / attributes`), with `trace_id`/`span_id` captured from the active OTel span at enqueue time (turn_span correlation). `event` value priority: `extra["event"]` → `"log"` (`label=` is plain payload and never names an event). `payload` = extra minus dedicated columns + `msg` (original text); when `logger.opt(exception=True)` is used, automatically merges traceback / exception_type / exception_value into payload (with a guard to distinguish real exceptions from loguru's empty RecordException `NoneType: None`). `source` = `extra["source"]` (default `"system"`).

## Registration

- **`diagnose` is always off.** Every sink goes through `add_sink`, which
  forces `diagnose=False` and refuses `diagnose=True`; the
  `lint-logger-add-diagnose` hook (`scripts/lint/diagnostics/logger_add_diagnose.py`)
  rejects any non-test `logger.add` without a literal `diagnose=False`,
  `add_sink`'s own included. loguru's default renders
  each traceback frame's local variables into the sink, which put a
  `RoleSecret` password and a password-bearing DSN into the journal and
  `release-executor.log`. `backtrace` (which frames show, never their values)
  stays at loguru's default.
- **stderr opens first.** `init_gateway_process` and `init_cli_process` open
  it before importing `base.paths`, because that import builds Settings, and
  the build logs (a runner continuing on a stale bootstrap snapshot warns).
  Fresh executable roots call `prepare_process_logging()` before configuration
  to remove Loguru's default handler; an embedded caller keeps its own sinks.
  After that fresh prelude, a record written before any sink exists is gone.

## Stdlib interception

`_StdlibInterceptHandler` routes records from stdlib `logging.getLogger(...)` into loguru sinks (many services historically used stdlib logging), otherwise their lines would only appear on stderr and not enter the event stream. Installed on the root logger; `psycopg.pool` recycling noise is gated to ERROR.

## Logging fixture restoration

`tests/stdlib_logging.py` restores logger levels as well as handlers. Levels
live in the process-global logger manager: a later test may format a bad record
that an earlier level gate discarded. The fixture restores root and previously
named levels, and resets newly created loggers to NOTSET; it does not assume
all third-party loggers began at DEBUG.
Only changed levels go through `Logger.setLevel`, which also invalidates
descendants' effective-level caches. An unchanged logger needs no reset: each
unconditional `setLevel` would clear the caches of every logger in the process.

Process initialization tests run each role in a fresh interpreter and inspect actual
JSONL files, stderr and event records. Repeated calls produce one record per sink
and no additional queue worker; cross-role calls share the process guard. Rotation
and retention tests use real files.

## Redaction before the handlers

`base/log/redaction.py` installs one process-wide loguru patcher — as the core patcher, so it runs
before every handler and the three sink types below all see the same masked record. It masks a
record on its way to disk only: an exec output a caller received back is unchanged.

Three rules, and nothing that guesses past them: the password in a URL's userinfo
(`scheme://user:password@` becomes `scheme://user:***@`; the user name is the identity the URL
dials as, so it stays); the value of a mapping key named exactly `token`, `secret` or `password`
(case-insensitive), including when that mapping was serialized into the message; and the value of
an environment assignment whose key is upper-case and ends in `_TOKEN`/`_PASSWORD`/`_SECRET`
(`MEIVA_API_TOKEN=...` becomes `MEIVA_API_TOKEN=***`). A key that merely contains one of those
words — `token_count`, `PASSWORD_POLICY`, `TOKENIZER_PATH` — keeps its value, because the line that
diagnoses a failure is usually the one next to the credential.

The patcher is installed by the seams that open a process's sinks (`start_process_logging` and
`init_subprocess_logger`), not by each sink. It must not reach the caller: what an agent's code
printed is evidence the agent and its logs have to agree on.

The rule cannot live in `base.host.net.url_text`'s `redacted_url`: the root manifest declares
`"log" = ["paths"]`, and `base.host` declares `base.log`, so importing it would close a cycle.
