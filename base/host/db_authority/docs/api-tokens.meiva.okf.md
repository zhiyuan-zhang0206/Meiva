---
type: doc
title: Machine API tokens
description: The write generation's machine API tokens that admit machine callers to the gateway API, bootstrap, webhooks and a unit's /ops, their launch delivery, and the telemetry relay token.
tags: [authority, auth, lifecycle]
---

# Machine API tokens

`api` is the HTTP half of the write generation: machine callers present a class
token instead of the human cluster secret. The generation secret carries one
32-byte token per class (`GenerationSecret.api`); the human cluster secret stays
on the gateway.

| Surface | Admits |
|---|---|
| gateway middleware, `/api/bootstrap`, alert webhooks | the human secret, or the ACTIVE generation's gateway or runner token (never a pending one; `acceptance`, cached per ledger identity in the gateway lifespan; `gateway.http.auth.request_principal.cluster_credential` records `cluster_bearer` or `machine_token:<class>`) |
| `/api/auth/login` | the web login password (a salted hash in `$MEIVA_HOME/web-login-password.json`; never the human secret), or the active runner token (the managed browser's cookie); the session is bound to that credential and ends when it stops being accepted or the password is set again. A one-time login ticket issued to the human secret opens a session bound to the secret instead, which ends when the secret rotates (`gateway.http.auth.request_principal.session_mints`) |
| a unit's `/ops` (`services/agent_runner/agent_ops/boot.ops_acceptance`) | digests of its generation's gateway and runner tokens: a remote unit's capability carries the gateway digest, never the gateway token |

The gateway checks the ledger identity on every request using its lifespan-owned
`machine_token_acceptance` cache; one-shot boot readers pass no cache and read
the store directly. Each lifespan starts with a fresh cache, whose entries are
keyed by home. A unit's `/ops` reads its acceptance
once, at daemon boot, which holds because nothing changes the accepted tokens
while a daemon runs. Delivery mirrors the login: the root launcher gives each service its class token (`api_access`,
`cli/commands/_data_plane.api_delivery`) only while the API is authenticated
(a set human secret on the gateway, an API-bearing capability on a remote
unit), so a delivered token always means "present a bearer"; the boot pass
gives an admitted operator process the same token. A pure runner's settings-lite
CLI reads only the API token from its installed capability when the process has
none and no launcher profile; exec children and watchers inherit delivered tokens. Clients present
`MEIVA_API_TOKEN` first (`base.cluster.machine.gateway_bearer`);
only an operator or gateway-profile process without one presents the human secret,
while a process launched with the agent or runner profile and lacking one raises
`GatewayApiTokenMissing` (a remote-managed plane delivers no token, so its gateway
home presents the human secret). Every gateway home has a secret, so every launched
service is delivered a token; a process holding neither a token nor the secret
raises `GatewayApiTokenMissing`.

The OTLP relay ingress is telemetry, not a write path: its bearer is a random token the gateway
home keeps in `db-authority/telemetry-token` (0600), minted by the first converge of a gateway
start and kept ever after (`ensure_telemetry_token`); remote units receive it in their
capability. Nothing derives it from the human secret or the write generation, so rotating
either leaves it, and every remote unit's capability, valid. To replace it, delete the file on
the gateway home, stop and start the gateway (its converge mints a new one) and issue every remote unit
a new bundle.

The human-bearer rotation (`scripts/data_plane_ops/rotate_cluster_secret.py`) keeps the
logical-backup passphrase pinned (`services/backup/artifact/passphrase.py`:
minted at birth, `sha256(secret)` for a home born earlier), then changes the
secret. No remote unit is affected: it holds a machine API token and the telemetry
token, neither of which comes from the human secret.
