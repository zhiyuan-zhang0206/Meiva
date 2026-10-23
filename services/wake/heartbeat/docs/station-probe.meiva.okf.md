---
type: doc
title: Remote station probe
description: Explicit endpoint inputs, authentication evidence and fail-open remote observations.
tags:
- healthchecks
- observability
---

# Remote station probe

The standalone entry receives its existing concrete `ConfigBoot` owner. Its
constructor performs no environment delivery or model build; the module wrapper
only constructs it. `main` captures the loaded image before booting that owner,
then derives the lazy machine identity and frozen endpoint inputs from its view.
It retains the same home, version gate and work/telemetry factory composition.

`services/wake/heartbeat/station_probe.py` resolves the remote observatory from
its frozen `StationEndpointConfig`. An unset or malformed URL skips the round
before database discovery. The shared `base.telemetry.station_endpoint` owner
selects a pure station's advertised ingress; a hybrid gateway/station advertises
its gateway URL and is not an ingress target. Without a station advertisement,
the validated configured base and explicit remote OTLP port form the fallback.
Collector rendering uses the same resolver and supplies its remote station port,
independently of this unit's local OTLP receiver port. Discovery failure skips
the observation rather than inventing an address during an outage.

Each HTTP observation receives the existing `UrlOpener` port and the telemetry
token read once by its caller from its retained unit home. The request is
`POST <target>/v1/traces` with an empty `resourceSpans` array, JSON content type,
`Authorization: Bearer <token>` and a five-second timeout. The probe never needs
the human cluster secret from which that published token was derived.

| Observation | `station_answers` result | Supervision verdict |
|---|---|---|
| Authenticated OTLP request accepted with 2xx | `True` | ALIVE |
| HTTP rejection, including 401/415/5xx | `False` | DOWN |
| Connection failure or timeout | `False` | DOWN |
| No published token | `None`, no request | UNAVAILABLE, before DB discovery |
| Target cannot be resolved | No HTTP observation | UNAVAILABLE |

An HTTP rejection proves only that a listener answered, not that authenticated
ingestion serves. Likewise a missing token cannot certify health. The
supervision adapter reads only its explicit home's published token and never
uses another unit's token or infers authentication from a nonempty human secret.

Standalone execution remains fail-open: missing credentials warn and skip;
failed observations warn without blocking, restarting or shedding local
business. The collector keeps buffering in its file-backed queue. The station
hosts the observability backends, so a station that cannot ingest cannot carry
its own failure alert.

Tests exercise advertised and fallback ports, hybrid exclusion, real published
tokens and explicit HTTP responses. They assert method, URL, body, bearer and
timeout, plus no database/network work for missing credentials. Standalone
healthy, failed and missing-token rounds preserve the same local-business
boundary; startup composition retains the shared captured image and DB gate.
The direct probe scenarios isolate process logging and background writer construction.
The separate shared-factory case observes their image, database closure and producer
arguments; isolation alone does not prove a real logger or writer lifetime.
