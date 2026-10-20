---
type: doc
title: "External Caller Profiles"
description: "Explicit subprocess provenance for external tools, separate from authenticated credentials."
tags:
- base
- identity
---

# External caller profiles

`MEIVA_CALLER_IDENTITY` is an opt-in JSON handoff inherited by a coding tool's
subprocess tree. `base.agents.messages.external_caller` validates it as an external-only
`CallerIdentity`; malformed profiles fail without falling back to human, system,
or inherited Meiva agent identity. The environment value is not a credential.

Example profile: `{"kind":"external_agent","subject":"codex","instance":"run-42"}`.
Claude Code uses subject `claude_code`. Set the profile once in the external
tool's launch environment, not separately for every generated command. Instance
IDs are short stable opaque identifiers of 1 to 20 ASCII letters, digits, dots,
underscores or hyphens (first character alphanumeric), for example `run-42`.
A raw 36-character UUID is not supported: choose a distinct short identifier;
the implementation never truncates and pretends it is the original session ID. Do not
place secrets, user names, paths, or prompts in the bounded instance field.

CLI send requires an explicit `--source` at the parse layer and never
consults the profile: an inherited `MEIVA_CALLER_IDENTITY` neither fills a
missing flag nor vetoes the explicit value. Restart, resurrect, terminate
and kill never consult the profile either; an explicit `--source` is
forwarded unchanged, and omitting it claims no provenance, leaving the
server default in place.
SDK source selection prefers real hosted-turn context, then an
explicit external profile, then its established legacy actor. An external shell
cannot become its Meiva parent merely by inheriting `MEIVA_AGENT_ID`.

## Sending as an external agent

`meiva agents send <id> <text> --source external_agent:<subject>[:<instance>]`
delivers the message as coming from that external agent; `--source user` delivers
it as the user. See [[base/api_contracts/docs/caller_identity.meiva.okf.md]] for
when to use which. The SDK path (`meiva.agents.send_message` from a process whose
environment carries the profile) stamps the profile's source the same way.

The declaration is not a credential, so it adds no authorization scope, token
store or idempotency namespace.

The gateway `/mcp` endpoint forwards `send_message` with `as_external_agent` to the HTTP
API as source `external_agent:mcp`; the API authenticates the caller like any other request.

The Codex and Claude launch wrappers accept `--caller-instance run-42` as an
explicit opt-in, placing a shell-quoted JSON profile only on the external child.
Omitting the flag leaves existing launch commands unchanged. Validate the
instance before touching files or creating sessions. Existing live canonical
Codex generations are adopted as-is; this option cannot retrofit a running
process environment.
