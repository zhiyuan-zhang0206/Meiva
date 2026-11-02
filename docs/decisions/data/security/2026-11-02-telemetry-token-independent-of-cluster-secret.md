# The OTLP telemetry token is minted at random, independent of the cluster secret

## Context

The OTLP relay ingress of a gateway or station collector authenticates every remote unit with one
bearer, the telemetry token. Until now it was `HMAC-SHA256(MEIVA_CLUSTER_SECRET, label)`
(`base/host/db_authority/api.py:telemetry_token`): a unit holding it learned nothing about the
secret, and no second secret had to be distributed. The 2026-10-03
[cluster-secret contraction](2026-10-03-cluster-secret-contraction.md) kept that derivation and
only moved who computes it (the root that holds the secret writes a private file the heartbeat
reads).

The derivation tied two unrelated lifetimes together. Rotating the human secret changed the
telemetry token, so every remote unit needed a newly issued capability bundle, stopped and
reinstalled by hand. That is why `scripts/data_plane_ops/rotate_cluster_secret.py` was defined as
"run only after a bearer leak": a routine rotation of a browser-facing password would have cost a
visit to every runner. The secret protects the gateway's human surface; the telemetry token
protects an ingress. Neither should hold the other hostage.

## Decision

The telemetry token is a random value minted once for the gateway home and kept in its private file
(`$MEIVA_HOME/db-authority/telemetry-token`, 0600). Nothing derives it.

- `ensure_telemetry_token(home, fresh)` publishes `fresh` only when the file is absent or blank;
  a published token stays as it is. The caller draws `fresh` (the repository's secret generator,
  `base.host.db_authority.ledger.new_secret_token`) and passes it in, so the function reads no
  random source and a test places any value.
- A gateway home publishes it in a converge step, `telemetry token`, ahead of the collector step.
  The collector step renders the token into the sidecar's config, so on a first start a publication
  made later (with the data plane) would render a collector without a remote ingress.
- `telemetry_token(cluster_secret)` and its label are deleted, and so is every `cluster_secret`
  parameter that only fed it: `issue_bundle`, `telemetry_bearer`, `resolve_ship_target`,
  `DataPlaneInputs`. `issue_bundle` reads the file and refuses a home that never published one.
  `telemetry_bearer(home)` is the installed capability's token, else the home's published file.
- The human-secret rotation script is no longer limited to leaks. It replaces the operator bearer
  and ends the browser sessions bound to it; remote units are not touched.

Existing homes need no migration: the file a running gateway published under the derivation is a
random-looking token to every reader, and the first start of this release keeps it, so every
installed capability and every collector config stays valid.

## Alternatives rejected

- **Keep the HMAC derivation.** Its one benefit (no second secret to distribute) already holds: the
  token travels in the capability bundle like the runner API token, so a second secret costs
  nothing to distribute. What the derivation costs is the coupling above: every secret rotation
  forces a reissue to every remote unit.
- **Derive from the write generation instead.** Write generations do not rotate any more
  ([retired](../../runtime/updates/execution/converge/2026-10-03-retire-write-generation-rotation.md)),
  and a derivation from it would tie the ingress to the database credential, whose exposure and
  rotation story is different.
- **Mint at `meiva init`.** It would need the token in the birth facts of every role and would leave
  a home born earlier (all of production) without one. A converge step covers both.
- **Draw the token inside `ensure_telemetry_token`.** It would hide a random read in a leaf and a
  test could not tell "kept" from "re-minted".

## Consequences

- A human-secret rotation is a gateway restart: no capability reissue, no unit restart.
- Nothing rotates the telemetry token on its own. To replace it (a leaked bundle or compromised
  unit), delete `db-authority/telemetry-token` on the gateway home, stop and start the gateway (its
  converge mints a new token and re-renders the collector), and issue every remote unit a new
  capability bundle.
- `issue-unit` refuses a gateway home that never started under this release, with a message that
  says to start it first, instead of sealing a bundle with an empty or wrong token.
- The earlier decision's statement that the telemetry token is derived from the secret is
  superseded; the rest of it (the secret stays with the gateway, the CLI and the root) stands.
