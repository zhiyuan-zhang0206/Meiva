# Run Tempo as a native LGTM backend

The observability station manages Tempo as an ordinary `meiva-root` unit
alongside Loki, Prometheus, and Grafana. The station capability or the
`lgtm-host` marker admits the service. This extends the
[native LGTM lifecycle decision](2026-08-23-native-lgtm-backend-lifecycle.md)
to the trace store and follows the [production alignment decision](2026-08-23-native-lgtm-prod-alignment.md)
by putting station defaults under version control. The root supervisor owns recovery and stops Tempo in the
same wave as Loki and Prometheus, after the collector has flushed.

Converge verifies upstream archive checksums and renders the local store, WAL,
listeners, and retention. The existing query and intake URLs supply the two
public protocol ports on the station. The native listen host supplies their
bind address and the internal gRPC address. The internal gRPC default of 9096
leaves 9095 available for the default Loki gRPC listener.

The station previously ran Tempo 3.0.2. We pin 3.1.1 because the earlier release
has no upstream Darwin arm64 binary. New blocks keep the 3.0.2 default
`vParquet4` format to ease rollback. The default 168-hour block retention and
600 MiB Go heap target preserve the station's investigation window and existing
Go memory setting. Root provides a 50-second TERM window, covering the
upstream worker's 30-second finish budget and the remaining shutdown work.
In the isolated Linux cutover test, Tempo 3.1.1 recovered a 3.0.2 WAL trace
and exited normally in 30 seconds after SIGTERM.

A successful `/ready` counts only after all three listeners are attributed to
the recorded native unit. The root verifies the persisted native process birth and boot ID before reclaiming
a prior generation. Startup checks configured binds among the selected backends,
then reports the address and PID of any overlapping external listener. Operators can stage assets while the legacy unit is serving,
then stop its pulse timer and retire the old unit before starting the managed
Tempo. Copy the complete stopped data directory into the native volume to
preserve blocks, WAL, and shutdown markers. Verify the authenticated collector
relay from every runner before retiring any direct OTLP/HTTP forwarding.
