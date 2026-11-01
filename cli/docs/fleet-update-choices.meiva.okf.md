---
type: doc
title: Fleet update choices
description: "The flags cli.fleet_update requires and the modes they select: target selection verified before any SSH, the stop and start choices forwarded verbatim, and the agent-update refusals."
tags: [cli, updates, operations]
---

# Fleet update choices

Each choice below is a required flag; a missing or conflicting one exits 2, all
listed together, before any host is touched or log written. The operator's stop
and start choices are forwarded unchanged, never checked against the diff.

- `down` / `run`, target selection (mutually exclusive modes, verified locally before any SSH):
  `--channel stable|latest` (verifies `refs/tags/channel/<name>` `meiva-channel/v1` pointer and
  its target `meiva-release/v1` tag certificate), `--release TAG` (`vYYYY.MM.DD.HHMMSS` or
  `-hf<N>` with a verified `meiva-release/v1` certificate), `--new SHA` (40-hex commit carrying
  a verified `meiva-release/v1` tag certificate), or `--track-branch BRANCH` (optionally paired
  with `--new SHA` for an ancestor commit on `origin/BRANCH`, printing
  `target: UNRELEASED branch <branch> @ <sha> (preview / rapid-iteration mode)`). Uncertified or
  tampered tags, pointers, or commits refuse with exit 2 before contacting any host.
- Both halves: `--runner ALIAS` (repeatable) or `--no-runners`; `--dry-run` or
  `--execute`.
- `down` / `run`, forwarded to every host's `meiva stop --yes`:
  `--keep-shell-sessions|--close-shell-sessions`, `--keep-browser|--stop-browser`,
  `--keep-extras|--stop-extras`, `--keep-service NAME` (repeatable) or
  `--keep-no-services`, `--mode smooth|force`, `--stop-timeout` (as `--timeout`),
  `--message TEXT` (non-blank natural-language explanation for live agents and
  closed busy shells, also appended to the Grafana silence comment) or
  `--no-message` (omitted from a host's stop command when its pre-switch
  checkout predates `--no-message`, while still appended to the silence comment).
  `--keep-infra|--stop-infra` goes to the gateway's stop only.
  `--yes` is always sent (non-interactive SSH).
- `down` / `run`, for the fleet itself: `--allow-python-change|--refuse-python-change`,
  `--allow-red-schedules|--refuse-red-schedules`, `--silence-hours`,
  `--lease-confirm-seconds` (0 reports the leases and never waits).
- Every half, forwarded to every host's `meiva start` (over SSH, or in the macOS
  one-shot LaunchAgent) by `up` and by a failed `down`'s recovery: `--all-services`,
  `--only-service NAME` or `--disable-service NAME` (repeatable), and `--start-timeout`.
- `up` / `run`: `--smoke-timeout`, `--roster-timeout`.

One start selection applies to every host.

All commands refuse an agent's inherited `MEIVA_AGENT_ID`. An authorized agent
can submit an OS-owned job and return before drain; the submitting shell is not
the updater. The [agent update guide](../../meiva_builtins/skills/platform/meiva-guide/ops/references/agent-update.md)
owns the macOS launchd submission recipe. The operator checkout and interpreter
remain unchanged throughout the update.

Parent: [[cli/docs/fleet-update.meiva.okf.md]].
