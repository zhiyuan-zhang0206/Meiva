# Complete fleet updates submitted by an operator agent

## Context

The source-mode updater exposed only `down` and `up` so an operator could insert
manual release work between them. Ordinary updates needed no intervening work,
but still required two invocations. An operator agent also needed an executor
that survived its own drain and replacement of the machine's PTY service.

## Decision

Add `python -m cli.fleet_update run` to execute the existing halves in sequence,
with their shared options, checks and one log. The first failure ends the run
and names the failed half for recovery. Keep separate `down` and `up` commands
for intermediate manual work and repair. Do not automatically retry or roll back.

An authorized Meiva operator agent may submit the command from its own shell to
a macOS launchd one-shot and then return so it can drain. The job runs from an
unchanged operator checkout and interpreter outside the cluster's lifecycle,
with file-backed logs and an exit-code receipt. Keep the existing refusal to
execute the fleet command directly in an agent shell. The guide owns the
[submission recipe](../../../../../meiva_builtins/skills/platform/meiva-guide/ops/references/agent-update.md).

## Consequences

Routine updates need one invocation and reuse the existing stop/switch/start
implementation. Launchd supplies executor ownership without a new updater
service or deployment state machine. Submission is not completion; the agent
or an external operator reads the recorded result. A failed update still needs
operator diagnosis and recovery, and a GUI LaunchAgent does not survive logout
or a machine reboot. Runtime rollout authorization remains separate from code
contribution.
