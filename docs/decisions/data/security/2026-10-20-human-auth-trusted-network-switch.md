# Human-facing authentication can be switched off on a trusted private network

## Context

On 2026-10-11 the unauthenticated posture and the `gateway.auth_middleware_enabled` knob were
removed: the cluster API always authenticated, and a blank cluster secret admitted nothing. That
decision treated every credential the gateway checks alike. They are not alike. Two groups exist:

- **Human-facing**: the web UI login (a session cookie) and an operator's cluster-secret bearer.
  A person types, pastes or stores them.
- **Machine**: the write generation's API tokens, the `/ops` digest, the telemetry token, the data
  plane's SCRAM credentials, webhook tokens and the `/mcp` endpoint's OAuth-style client tokens.
  They are delivered automatically and nobody notices them.

A deployment whose security rests on a private network (the machine declares a private address in
`MEIVA_MACHINE_HOST`, and the gateway listens on loopback plus that address only,
`base/cluster/port_preflight.py:bind_addrs`) pays the login cost for no extra protection.

## Decision

The user ruled on 2026-10-20 that human-facing authentication may be switched off by configuration.

- `gateway.human_auth` (`MEIVA_GATEWAY_HUMAN_AUTH`) is an enum: `required` (default, today's
  behaviour) or `trusted_network`. Any other value is a validation error.
- In `trusted_network` mode the auth middleware keeps its order: a valid session cookie, then a
  bearer (a machine token keeps its own credential fact). A request presenting neither is served
  as the cluster administrator with `source_verified_by = "trusted_network"`. That fact counts as
  a human credential (`HUMAN_CREDENTIAL_FACTS`), so the UI can mint MCP client tokens and login
  tickets. `/api/auth/check` answers authenticated for such a request, so the Gate and the UI skip
  the login page.
- A request admitted without a credential still faces the browser attacks a private network does
  not stop. A state-changing request carrying an `Origin` outside the allowed origins is refused
  (the cookie path's rule), and its `Host` must name a loopback alias, a host of an allowed
  origin, this machine's declared host or the gateway URL's host, so a DNS-rebinding page cannot
  drive the API under an attacker-controlled name. Credentialed requests are not checked.
- The gateway logs a warning at start while the mode is on.
- The mode is not writable through the config API: an authenticated caller must not be able to
  switch authentication off.

This partly overturns the 2026-10-11 "no open posture" decision, only for the human-facing
surface. A cluster secret must still exist (`require_cluster_secret` and the start checks are
unchanged): machine tokens and the telemetry token derive from it.

## Alternatives rejected

- **Turning off every credential, machine and data plane included.** Machine credentials are
  issued and rotated by the platform and no person ever sees them, so removing them saves no
  friction, and a second posture (every agent, runner and `/ops` path with a no-credential branch)
  would be more code and more attack surface for nothing. The data plane keeps SCRAM for the same
  reason, and `/mcp` keeps its own client tokens because they identify the calling client, which
  is not a login.
- **Reviving "blank secret means open".** It tied an access decision to a secret that other
  components still need, and a blank value is an easy accident.
- **Skipping the Origin and Host checks on a private network.** A private network keeps off other
  machines, not off the operator's own browser, which a hostile page can aim at the listener.
- **Reverting the 2026-10-11 commit.** Its design (no knob, empty secret admits nothing) is right
  for the machine surface and stays.

## Consequences

- The safety precondition is the operator's: `MEIVA_MACHINE_HOST` must be a trusted private
  network address, or loopback. Anyone who can open a connection to the listener is the
  administrator; the Host check only closes the browser path.
- Audit actors and idempotency principals of anonymous callers read `trusted_network` /
  `cluster:administrator`; individual people are not distinguished, as with the shared bearer.
- A client presenting an invalid bearer in this mode is served anonymously instead of getting 401.
