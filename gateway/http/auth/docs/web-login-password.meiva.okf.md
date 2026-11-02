---
type: doc
title: Gateway — web login password
description: The password a browser types to log in, kept apart from the cluster secret as a salted scrypt record, set by a human credential and bound to every session it mints.
tags: []
---

# Gateway — web login password

The password a browser types is its own credential, not the cluster secret. The secret sits in the
gateway `.env`, which agents and schedule scripts are built to read, so it cannot double as something
a person types into a page. `POST /api/auth/login` accepts only the password (or the active runner
token of a unit's managed browser) and refuses the secret like any wrong password. Decision:
`docs/decisions/data/security/2026-11-02-web-login-password-split.md`.

- **Record**: `$MEIVA_HOME/web-login-password.json` (0600, written atomically):
  `{"algorithm": "scrypt", "n": 32768, "r": 8, "p": 1, "salt", "hash"}`. The strict
  `gateway/http/auth/login_password.py` derives, parses and verifies it (constant-time compare); the
  shell `login_password_store.py` writes it and reads it once per file revision (inode, mtime, size).
  The password itself is never stored, and it never reaches `.env` or the config API.
- **Damaged record**: a record that is not exactly that object raises. The login and every cookie
  check answer 500 with the file named in the log, instead of reading as "no password set". A bearer
  caller is unaffected, and `PUT /api/auth/password` with it replaces the file.
- **No record**: every login is the same 401 `invalid_password` as a wrong password and counts
  against the same limiter. A browser still gets in through a one-time login ticket
  ([[web-sessions.meiva.okf.md]]), which stays bound to the cluster secret.
- **Setting it**: `PUT /api/auth/password {"password"}` behind `require_human_credential` (the
  secret as a bearer, a human session or a trusted network; a machine token gets 403), at least 12
  characters (typed 422 `password_too_short`). `meiva auth set-password (--prompt | --stdin)`
  forwards to it with the home's cluster secret and takes the password from no default source and
  never from argv.
- **Sessions**: the mint kind is `password`, over the SHA-256 of the record's bytes. Each write draws
  a new salt, so the digest changes even for the same password: setting the password ends every
  password session, the caller's own included. Sessions a ticket opened (the secret's `human` mint)
  and a unit's managed-browser sessions stay, and a rotated secret leaves password sessions alone.
- **Cost**: one scrypt derivation (about 50 ms and 32 MiB) per password login and per set, run off
  the event loop. A managed browser's runner-token login does not pay it: the runner token is tried
  first.

Parent node: [[web-sessions.meiva.okf.md|Gateway — web sessions & browser-origin policy]].
