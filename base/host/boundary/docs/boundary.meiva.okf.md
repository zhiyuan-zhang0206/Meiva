---
type: doc
title: Untyped-value boundary
description: The one place a value typed Any by a library, decoded from JSON, parsed by argparse or returned by a child process is narrowed to a checked shape.
tags:
- base
- typing
---

# Untyped-value boundary

`base/host/boundary/` holds the helpers every package uses to read a value its source types
as `Any`, so the claim is checked once and the reader holds a real type. Every module is standard
library only, so a script run as `python3 path/to/tool.py` imports them after putting the checkout
root on `sys.path`.

- `json_values.is_object_dict` / `is_object_list` (`TypeGuard`s): a decoded JSON or `jsonb`
  value is a `dict[str, object]` (every key a `str`, checked) or a `list[object]`, with the
  values still `object` until the reader narrows them.
- `untyped.object_of(function, *args, **kwargs)`: `function(...)` read as `object`, for a
  standard-library or stub call that returns `Any` (`json.loads`, `yaml.safe_load`,
  `httpx.Response.json`). The result is narrowed with `isinstance` or a guard above, never
  bound as `Any`.
- `fields`: typed reads from a `Mapping[str, object]` (a gateway answer, a GitHub row, a cache
  file). `int_field` / `str_field` / `bool_field` are for a field the record must carry (a missing
  key or another type is a `TypeError` naming the key); `optional_*_field` answers `None` instead.
- `options`: the one boundary from an argparse `Namespace` to typed values. A handler declares a
  frozen dataclass naming the attributes it reads and calls `options(args, Shape)` once;
  `attribute(args, name, kind)` reads a value the entry binds beside the flags and `raw` one that is
  only asked whether it was answered. Nothing is defaulted: a missing or mistyped attribute is an
  `OptionsError` naming every such field, a programming error rather than a usage error. The cli
  and the repository scripts both use it (`docs/conventions/engineering/cli-argument-discipline.md`).
- `process`: `stdout_text` / `stderr_text` read a finished child's stream as the `str` a
  `text=True` caller asked for (`Host.run` and `run_bounded` return `CompletedProcess[Any]`); a
  bytes stream is a `TypeError`. `stdout_bytes` / `stderr_bytes` are the same reading for a call
  made without `text=` (raw bytes, no newline translation); a text stream is a `TypeError`.

When the shape is known, prefer a pydantic model or a module-level `TypeAdapter` over these
helpers; they are for the value whose shape the reader checks by hand.

`cli/fleet_update/untyped.py`, `cli/commands/management/schedules_verify_read_rows.py` and
`cli/fleet_update/fleet_alert_silence.py` keep their own copies on purpose: they are shipped to hosts that
predate `base`, and their file headers say so.

Rows read from Postgres are not here: `base/db/rows.py` (`query_one`, `query_all`,
`count_one`) reads them into a frozen row dataclass.
