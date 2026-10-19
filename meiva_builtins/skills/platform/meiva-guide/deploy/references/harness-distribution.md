---
type: doc
title: Harness distribution across machines
description: How the external coding CLIs (claude, codex, agy, dsh) reach a second machine — the vendor install, the state that can be copied, and the login state that cannot.
tags: [deploy, harnesses, credentials]
---

# Harness distribution across machines

A second machine needs three different things, and they do not travel the same
way: the **binary** comes from its vendor, the **configuration** can be copied,
and the **login** may refuse to be copied at all. `scripts/onboarding/
harness_onboarding.py` keeps those three apart:

| Operation | Question it answers |
|---|---|
| `install` | How does this machine get a CLI, at a version I can name again? |
| `doctor` | What does the platform's own probe say about every machine, and where does it contradict the CLI? |
| `sync` | Which state of a configured machine can this one inherit — and which of it would break the source? |

```bash
# the matrix
.venv/bin/python scripts/onboarding/harness_onboarding.py doctor --verify \
    --hosts macmini company-mini company-air macbook-air ubuntu

# a fresh machine
.venv/bin/python scripts/onboarding/harness_onboarding.py install claude codex agy dsh --hosts company-mini

# state from the machine that is already set up
.venv/bin/python scripts/onboarding/harness_onboarding.py sync claude codex \
    --from macbook-air --to company-mini --dry-run
```

Hosts are `~/.ssh/config` names; `local` is the machine running the script.
Every command runs under one pinned PATH (`transport.PATH_PREAMBLE`) because an
ssh or agent session has no interactive profile, and a CLI that is installed but
invisible is indistinguishable from one that is missing.

## What "installed" has to mean

The platform probe (`base/agents/birth/harness.py`) looks on `PATH` first, then
in `_FALLBACK_BIN_DIRS` (`~/.local/bin`, `~/.npm-global/bin`, `~/.bun/bin`,
`~/.claude/local`, `/usr/local/bin`, `/opt/homebrew/bin`). An install that lands
anywhere else is invisible to the probe until the process PATH happens to
include it — which is why the catalog pins every harness to a vendor command
whose output directory is on that list.

| Harness | Official command | Pinned | Lands at | Needs |
|---|---|---|---|---|
| `claude` | `curl -fsSL https://claude.ai/install.sh \| bash -s -- <version>` | 2.1.290 | `~/.local/share/claude/versions/<v>` + `~/.local/bin/claude` | curl |
| `codex` | `npm install -g @openai/codex@<version>` | 0.163.0 | npm global prefix (`/opt/homebrew/bin/codex`, `/usr/bin/codex`) | node + npm |
| `agy` | `curl -fsSL https://antigravity.google/cli/install.sh \| bash` | 1.4.2 | `~/.local/bin/agy` (self-updating single binary) | curl |
| `dsh` | `npm install -g @deepseek-ai/dsh@<version>` | 0.1.5-rc.3 | npm global prefix | node + npm |

The version used is recorded on the machine that received it, in
`~/.meiva/onboarding/install-log.jsonl` — one line per install: what was asked
for, what arrived, by which method. `agy` publishes only a moving manifest
(`…/manifests/<platform>.json`), so its installer cannot pin; its log line is
the only record that the machine was deliberately set to 1.4.2.

## Credential stores

This is the part that does not generalize, so it is written down rather than
inferred. Every statement below was read off the machines on **2026-10-19**;
none of it is inferred from a vendor document.

| Harness | Store | Shape | Copy verdict |
|---|---|---|---|
| `claude` | `~/.claude/.credentials.json` (Linux, and macOS when the Keychain item is absent) | `claudeAiOauth.{accessToken, refreshToken, expiresAt, refreshTokenExpiresAt, scopes, subscriptionType}` | unmeasured — see below |
| `claude` | macOS login Keychain, generic password `service="Claude Code-credentials"` | same blob | not automated; the Keychain is per-machine state |
| `codex` | `~/.codex/auth.json` | `auth_mode="chatgpt"` → `tokens.{id_token, access_token, refresh_token, account_id}` + `last_refresh`; `auth_mode="apikey"` → static `OPENAI_API_KEY` | the API-key form is safe; the ChatGPT form is usable on a second machine immediately |
| `agy` | `~/.gemini/antigravity-cli/antigravity-oauth-token` | `token.{access_token, token_type, refresh_token, expiry}` + `auth_method` + `id_token` | **copies successfully** |
| `agy` | `~/.gemini/jetski-standalone-oauth-token` | same blob, written by the Antigravity **IDE** | not read by the CLI, but a usable source: `sync` retargets it to the CLI's own path |
| `dsh` | `${DSH_HOME:-~/.dsh}/.credentials.yaml` | a DeepSeek API key (`refs: DEEPSEEK_API_KEY`) | safe — a key is not rotated by being used |

### Where `agy` actually keeps its session

The platform probe reports `unknown` for Antigravity on every machine because it
looks only inside the CLI's config directory. The session is one level away:

```
~/.gemini/antigravity-cli/antigravity-oauth-token     # current (agy 1.4.2)
~/.gemini/jetski-standalone-oauth-token              # older location, same shape
```

Confirmed on a live process rather than by inspection: `lsof -p <agy pid>` on a
running `agy` shows file descriptor 15 reading
`~/.gemini/antigravity-cli/antigravity-oauth-token`.

The older `jetski-…` file is **not** a fallback the CLI reads. A machine that
carries only that file answers `Error: authentication required. Run 'agy' to log
in, then retry.` — measured on `macbook-air`, whose `agy` CLI has therefore never
been signed in, while its Antigravity IDE keeps the legacy file fresh. Treat the
two as different owners: the CLI owns `antigravity-cli/`, the IDE owns
`jetski-…`. `sync` copies from either and always writes the CLI's path.

## What was measured, and what was not

Measured on 2026-10-19 with two secondary machines, never the primary
(`macmini`) and never the user's live sessions:

| Experiment | Result |
|---|---|
| `dsh`: copy `${DSH_HOME}/.credentials.yaml` from macbook-air to company-mini | **works** — `dsh --profile headless "Reply with exactly: OK"` answered `OK` on a machine that had never held a DeepSeek key |
| `codex`: copy `~/.codex/auth.json` (ChatGPT mode) from macbook-air to company-mini | **works** — `codex login status` flipped from "API key" to "ChatGPT", and `codex exec` answered on the copy |
| `agy`: copy the Google token from macbook-air to company-mini (which had never run `agy`) | **works** — `agy -p "Reply with exactly: OK"` answered `OK`, with no browser login |

A fourth run waited for the copied `agy` access token to expire and then called
the CLI again:

| Experiment | Result |
|---|---|
| `agy`: wait until the copied access token expires (23:21), then `agy -p` on company-mini | **works** — the CLI refreshed on its own, answered, and moved its expiry an hour out; the `refresh_token` value on disk did **not** change |

What remains unproven is narrower than before: **whether a refresh evicts the
machine it was copied from.** The source of the `agy` copy turned out to have no
CLI login at all (see above), so it could not serve as the other side of that
test, and the codex access token still had about nine days left when it was
copied. `catalog.Store.rotation` therefore stays `unknown` for those stores and
`sync` withholds them unless the operator passes `--accept-credential-risk`.

## Copy verdicts

| Harness | Verdict | Basis |
|---|---|---|
| `dsh` | **safe to copy** | a static API key; the copy answered on the target |
| `agy` | **copyable, and the copy keeps itself alive** | copied to a machine with no Google login, answered, then refreshed by itself an hour later |
| `codex`, ChatGPT mode | **copyable and usable now; the refresh path is unobserved** | `codex exec` answered on the copy; no refresh happened, so eviction stays open |
| `codex`, API-key mode | safe to copy | a static key, like `dsh` |
| `claude` | **sign in once per machine** | the only live login is the primary's, its macOS copy lives in the Keychain, and no copy experiment ran against it |

## Choosing the golden machine

When a login can be copied, keep exactly one machine as the source of truth per
harness and copy from it — never sign in on each machine and then wonder which
copy is live:

1. `macmini` is the primary and holds the only live Claude login and a live
   `agy` session; it is the golden machine for anything it has.
2. A secondary machine is the golden machine only for harnesses the primary
   lacks (`dsh` lives on `macbook-air`).
3. A copy is taken from the golden machine, never towards it. A refresh on a
   copied login is expected to break whichever machine refreshes second, and
   that must not be the primary.

## Failure triage

| Symptom | Cause | Fix |
|---|---|---|
| `env: node: No such file or directory` from `codex --version` | the npm shim runs a Node script and `node` is not on the executing PATH | install Node (`brew install node` / `apt install nodejs npm`); the pinned PATH in `transport.py` already includes `/opt/homebrew/bin` |
| probe `unavailable` although the CLI answers | npm installed it under `/usr/bin`, which is not in `_FALLBACK_BIN_DIRS`, and the caller's PATH is trimmed | export PATH for that process, or install through a directory the probe searches |
| probe `available`, `claude auth status` says `loggedIn: false` | `~/.claude.json` keeps an `oauthAccount` record after the token expires or is revoked; the probe reads that record as evidence | treat `claude auth status` as authoritative until the probe stops treating `oauthAccount` as proof |
| `agy` reports `unknown` everywhere | the probe looks only inside the CLI's config directory | expected; use `doctor --verify` and a real `agy -p` invocation |
| `agy -p` says `authentication required` although `~/.gemini/jetski-standalone-oauth-token` exists | that file belongs to the Antigravity IDE; the CLI reads only `~/.gemini/antigravity-cli/antigravity-oauth-token` | copy the legacy file to the CLI's path, or sign the CLI in once |
| `sync` prints `WITHHELD …` | the store's rotation is `unknown` or `rotating` and no acknowledgement was given | sign in once on the target, or pass `--accept-credential-risk` knowing the source may be signed out later |

## What `doctor` is not

`doctor` runs the platform probe and, with `--verify`, two checks the probe
deliberately does not make: it runs `<binary> --version` (does the installed
file actually execute) and asks the CLI for its own login state. The two columns
are separate on purpose — a probe that must never start a process cannot also be
the authority on whether a login is currently valid. Rows where they disagree
are printed with the disagreement rather than resolved silently.

`--verify` runs `claude auth status` and `codex login status`, which read local
state and spend no quota. `agy` has no non-interactive status command, so its
`login` column stays empty and a real `agy -p` run is the only proof.
