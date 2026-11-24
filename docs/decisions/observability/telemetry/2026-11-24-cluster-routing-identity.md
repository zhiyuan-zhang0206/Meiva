# Cluster routing identity: `meiva.cluster`, derived from the telemetry token

Telemetry isolation between clusters compares a **routing identity**, not the
home's directory name. Every producer stamps `meiva.cluster` on its logs,
metrics and trace Resource; the gateway collector's `filter/cluster_allow`
drops records whose `meiva.cluster` differs from the collector's own. The
`cluster` attribute stays what it always was, the display label (the home's
basename) that dashboards, alert rules and historical data select on. This
extends [telemetry cluster isolation](2026-08-24-telemetry-cluster-isolation.md),
which used the label as both display and identity.

## Why the label could not be the identity

`home_label` is `Path(home).name`. `/home/a/.meiva` and `/home/b/.meiva` render
the same label, so a foreign cluster whose home shares the directory name
passed the collector filter and polluted the other cluster's backends; and a
runner whose home is not named like the gateway's was dropped by it.

## Decision

- **One value, no new storage.** `meiva.cluster` is the first 16 hex digits of
  `sha256("meiva.cluster/v1:" + telemetry token)` (`base.cluster.cluster_id`).
  The telemetry token is minted once by the gateway home and is the same for
  every unit of the cluster (the gateway reads `db-authority/telemetry-token`,
  a runner reads it from its capability), so every machine derives the same
  id with no stored name. This keeps the
  [path-only identity decision](../../runtime/hosts/2026-07-20-path-only-cluster-identity.md):
  there is no second, stored name to drift, and the capability schema is
  unchanged (it forbids extra fields, so a new one would break older runners
  during a rolling update).
- **Not `service.namespace`.** Metrics already set `service.namespace=meiva`,
  and Prometheus's OTLP translation builds `job` from it.
- **No migration step.** `cluster` is untouched, so alert rules, dashboards,
  `production_identity` and all historical Loki/Prometheus data keep matching.
  A record without `meiva.cluster` (an older producer, collector-owned
  filelog/infra signals) is admitted as before, so any update order across the
  fleet is safe. A unit that holds no telemetry token yet renders the pattern
  `.*` and admits any id.

## Token re-mint

The id follows the token. Nothing in the repository re-mints a published token
(`ensure_telemetry_token` keeps an existing one), but if an operator replaces
the file the id changes with it. The relay between collectors authenticates
with the same token, so a runner holding the old token is rejected by bearer
authentication before any filter runs: the filter cannot silently drop an
authenticated runner's data. The one place a mismatch can sit behind a working
connection is the gateway host itself, where producers reach the local
collector over loopback without the bearer (a new producer with the new id
against a collector config rendered from the old token, or the reverse). Every
record `filter/cluster_allow` drops is counted
(`otelcol_processor_filter_*_filtered{filter="filter/cluster_allow"}`) and the
`meiva-ops-otelcol-cluster-allow-drops` alert fires on any increase; the fix is
to converge the collector and restart the producers.

## Follow-up

The `cluster` label comparison in the filter remains as a second check. Once
the whole fleet stamps `meiva.cluster` it only rejects a runner whose home
basename differs from the gateway's; dropping it is a separate change.
`production_identity` still names production by `.meiva` basename; the routing
identity already stops such a home from polluting another cluster's backends.
