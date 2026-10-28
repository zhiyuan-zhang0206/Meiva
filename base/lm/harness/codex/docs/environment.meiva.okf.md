---
type: doc
title: Codex session environment
description: Explicit Codex session environments, credential filtering and capture timing.
tags:
- harness
- codex
---

# Codex session environment

Authentication is the machine's `codex login` (ChatGPT subscription). The CLI
prefers an API key found in its environment over the login stored in
`~/.codex/auth.json`, so an inherited key — a cluster-pinned one reaches every agent
process — silently moves a subscribed session onto per-token billing with no plan
quota. `auth.py` therefore *removes* `OPENAI_API_KEY`, `CODEX_API_KEY` and
`CODEX_ACCESS_TOKEN` from the environment the session hands the transport. Removal,
not an empty value: the CLI asks whether the variable is present, and an empty string
is still present (the Claude CLI treats an empty value as absent, which is why that
adapter blanks instead). `api_key_mode` is the one explicit way to keep the key.

`CodexSession`, the `resolved_binary` helper in `session.py` and `StdioAppServer` require an
explicit environment mapping. The host captures the runner environment at its
existing configuration root; the bridge rebuilds PATH and adds a fresh registration
handle whenever it constructs a session, including a reconnect. Session construction
resolves the executable, filters credentials and copies the mapping into the
transport. It does not spawn the CLI until `start()`, which uses that snapshot
without inheriting the starting process's later environment. An explicitly empty
transport environment stays empty. HOME, CODEX_HOME, MEIVA_HOME, PATH and registration
values otherwise pass through to the CLI and its descendants.

Executable discovery remains a separate resource operation in the shared
`harness_binaries` owner: a missing PATH retains its existing host lookup, and
installation directories remain relative to the host home. Requiring the session
environment does not change that discovery contract or the other harness adapters.
