# Inspector Activity section

The panel's last section and its two grids. The shell (layout, order, close
surfaces) is in [Inspector Panel](inspector.md),
the section list it hangs off in [section details](section-details/section-details.md),
and the reads behind it in [reads and refresh](queries-and-refresh.md).

Two grids. Cost is **two blocks, one per billing mode**, each naming its mode in
the label and printing the amount alone at four decimals: `Cost (subscription)` is
the list value a subscription covered and `Cost (API)` the metered money the
account is owed (user ruling 2026-11-08, superseding the 2026-10-28 single block
whose amount carried a `(Subscription)` suffix). **A block is rendered only when
its amount is above zero**: zero has no block at all — a mode the agent never ran,
and a mode whose calls were all unpriced, both vanish whole (405 restated the
ruling the same day: the panel must not answer "$0.0000" to a question about
cost). One agent can show both, because a restart can move it to another harness
while the ledger keeps every call under its id. An `N unpriced` sub-line appears
under a block that does show when that mode has calls without a stored usage-time
price snapshot: those contribute 0 and are surfaced rather than hidden, never
silently partial. Then LLM calls; tokens in / out combined; cache-hit
percent (`N/A` when unmetered). Activity: TPS (the LLM stage's),
then the absolute durations the header window covers — LLM output and code execution
(the former Idle block was removed per user ruling 2026-11-09). For external
harnesses (`harness !== "meiva"`), TPS,
LLM output, and code execution are omitted because those belong to the native
`execute_code` loop. Without life (`alive_seconds = 0`) the duration cells show em
dashes while TPS stays visible. Durations use the shared tiering — `45s` / `15m` /
`1h` / `1h 30m` / `24d 3h` (day tier past 24h) — and TPS renders one decimal, `0` →
em dash. Token counts roll `≥1000M` to `B` and past `1000B` to `T`. The section
header carries the window picker and the ⟳
([reads and refresh](queries-and-refresh.md)).

Parent: [Inspector Panel](inspector.md).
