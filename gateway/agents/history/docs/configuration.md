# History configuration owners

Each history request builds its read inputs (`TimelineRead`, `UnderstandingRead`, plain values) from the `GatewayConfig` its gateway was built with (page size, compact depth, timestamp switch, `ClockConfig`, models); a changed setting reaches a gateway at its next restart, like every field of that config, and two gateways never share values. Gateway-profile understanding options read the same authority's environment file rather than constructing an agent domain; which source wins is the pure `understanding_enabled` / `understanding_chunk_ratio` in `read_inputs.py`. Window and cursor rules are pure functions (`paging.py`, `cursor.py`); `timeline.py` only reads checkpoints and renders.

Record-backed timeline reads and Quote admission call
`gateway.record_activity.settle_record_activity(request, agent_id, window)` after
awaiting the record RPC. This shared gateway owner re-reads canonical agent status:
`running` preserves pending tool activity; `idling` and `terminated` settle it.
The record reader owns completeness and anchor validation. An inclusive window
ending at the last complete record row retains pending activity just like the newest timeline
window, including when partial bytes follow that row; a historical window with newer rows remains settled. A partial JSONL
line or non-DONE Antigravity planner response cannot become a Quote source by
settling activity.
