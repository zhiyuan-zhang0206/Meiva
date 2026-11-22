# Fleet collaboration

The Fleet plugin contributes collaboration guidance to native agent prompts and external
harness handoffs. Core `meiva.agents` owns the agent directory, messaging and lifecycle.
Core `meiva.ui.ask` delivers questions and keeps them open until the user answers or closes them.

## Coordination

- Use `meiva.agents.send_message` to assign work, report progress or hand off artifacts.
- Use `meiva.self.terminate(message=...)` to exit with a result for the nearest living
  birth ancestor. Message the delegator directly when someone else assigned the work.
- Use schedules for recurring checks and time-based follow-up. Track repository bugs and CI
  failures in GitHub Issues. Record durable context in the workspace's `memory/MEMORY.md`.

See [agent messaging](neighbors/agent_messaging.md), [agent lifecycle](spawn.md),
[questions](questions.md), and the [Fleet skill](../skills/meiva-fleet/SKILL.md).

## Configuration

`FleetConfig.reduce_context_switch` controls the additional guidance for reaching the user.
The gateway owns the cluster setting and delivers it in the bootstrap snapshot. A new agent
turn reads the setting from its bound Fleet configuration.
