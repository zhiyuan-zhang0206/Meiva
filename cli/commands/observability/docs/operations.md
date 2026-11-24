# Observability operations

Current delivery, retention and failure contracts belong to their component owners:

- [Native LGTM lifecycle](lgtm/lgtm.md)
  and [backend configuration](../../../../deploy/lgtm/README.md).
- [OTLP producer export](../../../../base/telemetry/otlp/docs/telemetry-otlp/telemetry-otlp.md),
  [trace delivery topology](../../../../base/telemetry/otlp/docs/telemetry-otlp/delivery-topology.md),
  [bounded queues and loss](../../../../base/telemetry/otlp/docs/telemetry-otlp/export-backpressure.md).
- [Infrastructure metrics](../../../../base/telemetry/otlp/docs/telemetry-otlp/infra-metrics/infra-metrics.md)
  and [event labels](../../../../base/telemetry/otlp/docs/telemetry-otlp/event-resource-labels.md).

## Change backend selection

On the observability station, `meiva observability status` and `meiva status` report root-owned
readiness. Configuration or service-selection changes require a normal stop
before start; stored observation data survives. `meiva observability on` and `meiva observability off`
take the operator's explicit service selection, like `meiva start`; it must include the
four backends for `on` and must not name them for `off`.
Inspect the rendered configuration and pinned versions through the backend owner
above. Disabling the backend also removes the live data source for its consumers;
check those consumers before choosing an outage window.

`MEIVA_TELEMETRY_OTLP_ENABLED=false` disables producer export;
changing this startup-applied setting requires restarting the affected processes.
The collector's own infrastructure scrapes have an independent role gate. To
stop that service, use `meiva start --disable-service otel-collector` through normal
explicit start selection, rather than treating the producer switch as a
collector stop.

## Verify indexed event labels

**Event-label canary.** After an OTLP-emitter rollout, query a post-rollout
window and require every event stream label to equal its JSON event name. This
checks the indexed read path, not merely content filtering:

```bash
.venv/bin/python - <<'PY'
from datetime import UTC, datetime, timedelta
import json
from urllib.parse import urlencode
from urllib.request import urlopen

end = datetime.now(UTC)
params = urlencode(
    {
        "query": '{service_name="unknown_service"}',
        "start": str(int((end - timedelta(minutes=15)).timestamp() * 1_000_000_000)),
        "end": str(int(end.timestamp() * 1_000_000_000)),
        "limit": "2000",
    }
)
with urlopen(f"http://127.0.0.1:3100/loki/api/v1/query_range?{params}") as response:  # noqa: S310 — loopback Loki canary
    result = json.load(response)["data"]["result"]

mismatches = [
    (stream["stream"].get("event_name"), json.loads(line).get("event_name"))
    for stream in result
    for _, line in stream["values"]
    if stream["stream"].get("event_name") != json.loads(line).get("event_name")
]
observed_rows = sum(len(stream["values"]) for stream in result)
assert observed_rows >= 1, "No event rows observed; an empty sample cannot certify indexed labels"
assert not mismatches, mismatches[:20]
print(f"checked {observed_rows} event rows; no label mismatches")
PY
```

An empty sample cannot certify labels and fails the canary. An absent
`event_name` label or any mismatch also fails; run it only over
newly emitted rows, since indexed-era data from before the rollout is immutable.

For bounded trace investigation, use [troubleshooting](../../../../meiva_builtins/skills/platform/meiva-guide/troubleshooting/SKILL.md).
