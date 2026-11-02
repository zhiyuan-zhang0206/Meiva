# The web login password is its own credential, stored only as a hash

## Context

`POST /api/auth/login` compared the typed password with `MEIVA_CLUSTER_SECRET`, so the human bearer
of the gateway API was also the string a person typed into the login page. That secret sits in
plaintext in the gateway `.env`, and the design leaves it readable to agents and schedule scripts
on the gateway host: they build the gateway's own environment and may read the file. A credential
that every agent can read is a poor thing to also type into a browser form, a password manager and a
shell history, and a person could not choose or change it: it was the minted token, and replacing it
was a control-plane rotation (`scripts/data_plane_ops/rotate_cluster_secret.py`), not a password
change.

The contraction of 2026-10-03 had already moved every non-gateway component off the secret; the web
login was the one human-facing use left that put it in front of people.

## Decision

The web login password is a separate credential with its own record, and the cluster secret is no
longer accepted as a login password.

- **Storage.** `$MEIVA_HOME/web-login-password.json` (0600, written with `write_private_bytes`):
  `{"algorithm": "scrypt", "n": 32768, "r": 8, "p": 1, "salt", "hash"}`, `hashlib.scrypt` from the
  standard library (no dependency), a 16-byte salt drawn from the gateway's secure generator. Only the
  hash is kept: the password exists in clear text only in the request that sets it. Parsing, deriving
  and comparing (constant time) are pure functions in `gateway/http/auth/login_password.py`; reading
  once per file revision and writing are the shell `login_password_store.py`. A record that is not
  exactly that object raises instead of reading as "no password set".
- **Login.** `POST /api/auth/login` verifies the typed password against the record, or accepts the
  active generation's runner API token (a unit's managed browser, unchanged). With no record it
  answers the same 401 `invalid_password` as a wrong password and counts against the same limiter.
- **Sessions.** A new mint kind `password`, whose credential digest is the SHA-256 of the record's
  bytes. Setting the password writes a new salt even for the same password, so the digest changes and
  every password session ends with no revocation step, as a rotated secret ends the `human` ones.
- **Setting it.** `PUT /api/auth/password {"password"}`, behind `require_human_credential` (the secret
  as a bearer, a human session or a trusted network; a machine token gets 403), at least 12
  characters (typed 422 `password_too_short`). `meiva auth set-password (--prompt | --stdin)` forwards
  to it with the home's cluster secret and chooses no default source; the password never goes in argv.
- **Tickets stay on the secret.** `POST /api/auth/login-ticket` still binds the `human` mint, so the
  one-time link `meiva start` prints opens a session on a gateway whose password was never set, and
  a password set again does not end a ticket's session. The secret remains a bearer and the root of
  that link; it is no longer a thing a person types.
- **Callers that logged in with the secret** (`scripts/verify/observe.py`, the e2e browser contexts)
  trade the secret for a ticket and redeem it, as the printed link does.

## Alternatives rejected

- **Keep one credential for both.** It is the status quo and the cheapest, but it makes the one
  credential that must stay out of agents' reach the one a person has to type, and ties choosing or
  changing a password to rotating the control plane. A password anyone can choose and change, with the
  secret kept as a bearer, is the property being bought.
- **Treat Cloudflare Access (or any front proxy) as the only identity layer and drop the login
  password.** The gateway is reachable only over the private network and has no public ingress (the
  Cloudflare Tunnel was retired), so there is no identity layer in front of it to lean on; the browser
  login is the gateway's own admission and must hold by itself. An operator may still put a proxy in
  front of it, but the product cannot assume one.
- **Put the password in plaintext in `.env`** (a `MEIVA_WEB_PASSWORD` next to the secret). It is
  simple to set and to read back, but it puts a second typed-by-humans credential where agents read,
  repeats the exposure this decision removes, and makes it a config field the settings panel and the
  config API would serve. A hash in an owner-only file leaves nothing to read back.
- **Derive the password session from the cluster secret** (a keyed digest of the typed password).
  It would keep sessions tied to the secret, but then the secret is again required to verify the
  password and rotating it logs everyone out for a reason that has nothing to do with the password.
- **A password file the CLI writes directly** instead of an API. It would work with the gateway down
  but skips the length rule and the invalidation that live in the gateway, and a CLI that edits the
  credential store is a second writer. The CLI is a thin forwarder; the gateway owns the record.

## Consequences

- **Rollout.** After the gateway runs this change, the cluster secret no longer logs a browser in.
  Run `meiva auth set-password --prompt` once on the gateway home. Until then, the ticket link `meiva
  start` prints (a terminal start with the UI launched) is the way in. Sessions bound to the secret's
  mint before the change are `human` sessions and stay valid until they expire; sessions a ticket
  opens from now on are the same kind.
- **A damaged record is loud.** The login and every cookie check answer 500 until the file is
  repaired or replaced (`PUT /api/auth/password` with a bearer still works, since it needs no
  session). That is the intended trade against silently treating a corrupt record as "no password".
- **Cost.** One scrypt derivation (about 50 ms and 32 MiB) per password login and per set, run off the
  event loop. A managed browser's runner-token login does not pay it: the runner token is tried first.
- **Clients that posted the secret to `/api/auth/login` stop working.** The repository's own callers
  were migrated to tickets; a local plugin or script that did the same has to do likewise, or use the
  password. The desktop app's native auto-login (`ui/app`) posts the secret and is a follow-up.
- A rotated cluster secret ends the ticket-opened sessions and leaves password sessions alone, so the
  two credentials now rotate independently.
