# Bounded shell-height diagnostic

The reported all-column Chrome shortening lacked incident-time browser readings.
A supplied short visual height demonstrated a sufficient application pin path;
isolated native Chrome probes were negative. No causal fix follows from that
result. We chose anomaly-only atomic geometry to distinguish a stale html inline
pin from a shortened internal shell during a future occurrence.

The existing scalar telemetry value truncates at 64 characters. Encoding a
snapshot there loses evidence; splitting fields into separate events permits
partial snapshots under the shared rate limit. The approved narrow extension
adds typed geometry to the existing batch/event/Loki path, keeping its transport
and volume guards. No new collector, storage, settings UI or dashboard is needed.

A document lifetime cap of three incidents and six records, a 60-second cooldown,
and one ten-second expiry bound collection. The second record prefers an observed
recovery over next-frame ordering evidence because the reported symptom can heal
later. Hiding or unmounting cancels pending work without refunding the budget.
An event-driven observer avoids continuous polling, accepting that metric-only
silent changes and pure paint faults can remain invisible. Diagnostic desktop
filtering is deliberately separate from keyboard policy, which is unchanged.
