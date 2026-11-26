# Runner bootstrap configuration mirror

An enrolled pure runner requests `GET /api/bootstrap?mirror_machine_host=<host>`
with its delivered runner API token. The gateway signs its usual cluster
projection for that enrolled host and the `agent-runner` role. Calls without
the query parameter keep the flat response used by older runners and enrollment.
The field registry still determines the projection. The database endpoint is
credential-free. Database logins and API tokens come from the installed unit
capability.

The envelope records schema version 1, the SHA-256 generation of the canonical
values, the issuer, machine host, role, issue and expiry times (Unix seconds),
and an HMAC-SHA256 integrity proof. The issuer identifies the cluster's runner
credential generation by a domain-separated HMAC. Runners holding the same
generation's token are within the existing shared trust boundary. Host and role
checks prevent accidentally using a mirror delivered for another unit.
Rotation of the locally delivered token invalidates mirrors signed under the
previous token. The mirror contains configuration secrets and is written
atomically as a regular `0600` file at `$MEIVA_HOME/run/bootstrap-mirror.json`.

A fresh mirror supplies configuration without a bootstrap request. At the configured
`BootstrapFetch.snapshot_fresh_s` threshold, startup attempts to refresh it
online. A transport failure or HTTP 5xx permits use of a still-authorized
mirror. A pure runner using that mirror can proceed through start after data
plane admission and registration if its one gateway probe reports an outage.
Credentials rejected by the gateway fail the refresh or preflight. The
authorization lasts 24 hours to cover a day of maintenance, and the issue clock
allows up to 30 seconds of forward skew. Expired or invalid mirrors need a
fresh, valid signed response. An invalid response fails the refresh. Local
publication failure keeps the prior mirror and reports the write failure while
this boot uses the verified live response.

During a mixed-version rollout the recognized v4 `bootstrap-snapshot.json`
is read under the previous freshness and outage rules when the mirror is absent.
The legacy writer publishes with mode `0600`. Both formats coexist in separate
files for older processes. Before publishing the first mirror, the writer persists an owner-only
`$MEIVA_HOME/bootstrap-mirror-enrolled` marker outside the run directory.
A missing, expired or invalid mirror after enrollment requires a new signed
response.
The boot result exposes `live_bootstrap`, `authorized_mirror`, or
`legacy_fallback` and the signed generation when present. Failed authorizations
surface as `invalid` or `expired` on `BootstrapFetchError.mirror_state`.

For pre-stage, fetch a signed response for the target runner while the gateway
is online and supply it to `stage_config_mirror` with the target home, delivered
runner token, machine host, and current time. Use `validate_config_mirror` to
check the same contract before publishing, or `read_config_mirror` to check
what was installed. Treat the downloaded response as private configuration.
