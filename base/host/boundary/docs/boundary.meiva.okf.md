---
type: doc
title: Untyped-value boundary
description: The one place a value typed Any by a library or decoded from JSON is narrowed to a checked shape.
tags:
- base
- typing
---

# Untyped-value boundary

`base/host/boundary/` holds the two helpers every package uses to read a value its source types
as `Any`, so the claim is checked once and the reader holds a real type.

- `json_values.is_object_dict` / `is_object_list` (`TypeGuard`s): a decoded JSON or `jsonb`
  value is a `dict[str, object]` (every key a `str`, checked) or a `list[object]`, with the
  values still `object` until the reader narrows them.
- `untyped.object_of(function, *args, **kwargs)`: `function(...)` read as `object`, for a
  standard-library or stub call that returns `Any` (`json.loads`, `yaml.safe_load`,
  `httpx.Response.json`). The result is narrowed with `isinstance` or a guard above, never
  bound as `Any`.

When the shape is known, prefer a pydantic model or a module-level `TypeAdapter` over these
helpers; they are for the value whose shape the reader checks by hand.

`cli/fleet_update/untyped.py`, `cli/commands/management/schedules_verify_read_rows.py` and
`cli/fleet_alert_silence.py` keep their own copies on purpose: they are shipped to hosts that
predate `base`, and their file headers say so.

Rows read from Postgres are not here: `base/db/rows.py` (`query_one`, `query_all`,
`count_one`) reads them into a frozen row dataclass.
