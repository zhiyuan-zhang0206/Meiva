---
type: doc
title: logger_add_diagnose — sink placement and diagnose
description: The two rules over `logger.add(...)` — the call lives in `base/log/`, and it passes a literal `diagnose=False` — and why both are needed to keep traceback frame variables out of every sink.
tags:
- scripts
- lint
- observability
---

# logger_add_diagnose — sink placement and diagnose

`scripts/lint/diagnostics/logger_add_diagnose.py`, wired into pre-commit as
`lint-logger-add-diagnose`. It owns two rules over every `logger.add(...)` whose
receiver is a `logger`-named object in any case (`logger`, `_logger`,
`self.logger`, `LOGGER`, ...). Test code (`tests/`, `test_*.py`, `*_test.py`) is
exempt.

## Diagnose

Every such call must pass a literal `diagnose=False`. loguru's `diagnose`
defaults to True, so an unmarked sink renders every local variable of a
`logger.exception(...)` failure's frames — DSNs, tokens, passwords — into that
sink's output. Missing the kwarg, a non-`False` literal, or a value this script
cannot verify statically (a name, a `**kwargs` unpack) are all flagged,
fail-closed; no inline exemption exists.

## Placement

Every such call must live inside `base/log/`, whatever it passes for `diagnose`.
`base.log.sinks.add_sink` is the one funnel: it forces `diagnose=False` and
installs the redaction patcher, so a sink registered beside it gets neither. It
is also the shape no runtime hook can reach — [[base/log/docs/sinks.meiva.okf.md|log sinks]]
disables frame-variable rendering on the handlers that exist when `base.log` is
imported, and `add_sink` does the same for the ones registered since, but a
direct `logger.add` after startup runs before any of our code formats a record.
The rule is a directory rather than a name so a second sink module inside
`base/log/` needs no exemption to review.

It takes the repository-relative path as an argument rather than deriving it: the
scan passes the path it already computed, and a test passes a made-up one.
