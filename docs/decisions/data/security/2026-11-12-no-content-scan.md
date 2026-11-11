# Meiva does not scan or mark the content that enters an agent's context

Status: decided 2026-11-12 by the project owner ("delete all security-related functionality" of
the content scan).

## Context

Meiva carried a prompt-injection scan for ingested content, built in three layers:

- A rule scan (`meiva/security.py`, `base/agents/messages/injection_scan.py`): 21 substring and
  keyword rules (forged tool-call markup, override phrases such as "ignore previous instructions",
  zero-width characters, HTML comments with instruction words). It ran at the SDK's ingestion points
  (`meiva.files.read`, `meiva.shell.run`, `meiva.shell.sessions.capture`, MCP tool results,
  `meiva.agents.get_last_message`, `meiva.web` search and fetch, `meiva.understand`, the `meiva_code`
  context files, both `meiva_memory` index notes) and on inbound chat and system notes in the claim node.
- A decision-model check of fetched page text in `meiva.web.fetch` (`MEIVA_WEB_FETCH_INJECTION_*`).
- On a hit, a SECURITY system note (`Content from <source> may contain prompt injection ...`) behind the
  result or the message, a warning prefix on the memory notes, and a `[security-flagged]` pointer in
  place of a skill description in the capability index. The content itself was never altered or blocked.

The two functions `meiva.security.scan_content` and `meiva.security.is_flagged` were also importable
by agent code, though not listed in `help(meiva)`.

## Decision

All of it is deleted: the module, its ingestion-point calls, the web model check and its settings,
the claim-side scan, the SECURITY note writer, the exec child's `security_findings` channel with its
after_exec hook, the `security_scan_enabled` setting, and the skill index withholding. External content
reaches the agent unchanged and nothing marks it.

## Why

- It never blocked anything. A hit only added a note, so it could lower the odds of an injection
  working, never stop one.
- It matched keywords. A reworded or translated injection, or one in an unscanned path (skill scripts
  that fetch on their own, the Chrome MCP, shell sessions' live output), passed untouched, and the
  false-positive side was real for documents that discuss prompts.
- Its name and its setting (`security`, `security_scan_enabled`, "Scan inbound messages for prompt
  injection") promised more than it did, which gives a false sense of safety. The security model in
  [host isolation, not sandbox](2026-07-29-security-model-host-isolation-not-sandbox.md) says the
  boundary is the deployment host; a keyword scan inside the process does not change who can do what.
- The real premise of a cluster is that the agents in it trust each other and the user's content. The
  scan's cost was ongoing: a delivery protocol across three processes, a checkpointed state channel,
  a paid model call per fetched page, and notes in the transcript.
- The owner's earlier ruling (`future/infra/security/security-posture-debt.md`, before this change) kept
  the scan switched off on the production cluster: little web browsing, low injection worry.

## Alternatives rejected

- Keep it but not as an SDK entry (move it to an internal module): keeps the cost and the false sense of
  protection for no boundary.
- Strengthen it (blocking, model-based everywhere, reader/actor separation): a different project; it needs
  a sandbox to be real (see the prompt-injection map in `future/infra/security/prompt-injection.md`) and
  conflicts with the non-goals on sandboxing and per-agent authorization.

## Consequences

- External content is no longer marked. Agents and people see no SECURITY note. Skill descriptions are
  indexed as written.
- Kept: `NoteTag.SECURITY` and its timeline and UI literals (historical checkpoints carry the value, same
  as `MEMORY_WRITE_CHECK`); `SecurityFindingEntry` is registered as a retired checkpoint type
  (`RETIRED_CHECKPOINT_MSGPACK_TYPES`) so the restore census tells deleted from unknown.
- Not touched by this decision: the install-time skill scan (`base/packages/skills/scan.py`), the
  database role grants, gateway authentication, secret scanning in CI, `schedule_capability` and the
  credential filtering of the shell environment.
- The skill trust tier is a recorded label with no runtime reader; deleting it needs a database
  migration and an API change and is a separate decision.
- Stale `MEIVA_SECURITY_SCAN_ENABLED` and `MEIVA_WEB_FETCH_INJECTION_*` keys in a `.env` are ignored.
