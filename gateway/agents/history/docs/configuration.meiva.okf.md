---
type: doc
title: History configuration owners
description: Current ownership boundaries and their executable evidence.
---

# History configuration owners

Each history request builds its read inputs (`TimelineRead`, `UnderstandingRead`, plain values) from the `GatewayConfig` its gateway was built with (page size, compact depth, timestamp switch, `ClockConfig`, models); a changed setting reaches a gateway at its next restart, like every field of that config, and two gateways never share values. Gateway-profile understanding options read the same authority's environment file rather than constructing an agent domain; which source wins is the pure `understanding_enabled` / `understanding_chunk_ratio` in `read_inputs.py`. Window and cursor rules are pure functions (`paging.py`, `cursor.py`); `timeline.py` only reads checkpoints and renders.
