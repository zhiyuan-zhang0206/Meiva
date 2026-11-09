# Agent

## What it is

Overview of the Agent subsystem.

## Terminology (domain ubiquitous language)

> DDD ubiquitous language: the same word means the same thing in code / docs / PRs / conversations. These definitions
> are defined here as the authoritative terminology; check here before adding new terms.

- **Agent** — one OS process = one LangGraph `thread_id` = one conversation / task execution unit. Has a unique
  `agent_id` (int, `str(agent_id)` is the thread_id); runs the same graph (after_init→init_context→claim→before_llm→llm→before_exec→exec→after_exec self-loop, plus `harness_turn` for an agent whose loop runs in an external CLI).
  Agents are **equal peers**, OS-level isolated — child agents are **not** child processes of the parent, but detached,
  reparented to init as independent processes; one dying does not affect others.
- **Mode (not a framework concept)** — "one-shot vs persistent" are **not** framework-level modes, just differences in
  the **initial prompt template** used at spawn: there is no mode field in the graph. Adding a new agent type = writing a
  new initial prompt, no framework change.
- **Lifecycle verbs** (state enumeration / wire format see [cross-process contract](../../base/docs/agents-contract.md),
  inbound kind see [database layer](../db/docs/db.md), implementation in `base/agents/birth/spawn.py` + `ops/agents/wake.py`) — the distinction is
  "whether the new process needs to be told what it went through":
  - **spawn** — create new agent, **no inbound message delivered** (from nothing, no "why was I called" issue).
  - **resurrect** — bring a `terminated` agent back (history preserved), deliver a `kind='resurrect'` marker
    telling the model "you are resurrected" rather than continuing the previous context. Any new message to a
    terminated agent resurrects it the same way (there is no closed state).
  - **fork** — new agent + inherit state of a checkpoint (including history), deliver `kind='fork'` identity marker
    correcting "who I am".
  - **terminate / force** — graceful exit (deliver `kind='terminate'`, graph goes to END, process exits naturally)
    or request interruption of the active hosted turn when stuck (`force=true`, not available on `meiva.self.terminate()`);
    `enqueued` confirms acceptance, not exit or completion of owned work. With `kill_all_shell_sessions`
    every shell session the agent owns on its home machine (watchers included) is killed too, silently — right
    before a graceful termination applies, or at once for force (swept again when the host observes the force
    quiescent) / an already-terminated agent — so none of them can wake it again; the response's
    `shell_sessions` reports it.
  - **heartbeat** — check-in for an idle agent; claim appends a system note unless a permanent-provider circuit breaker is open, in which case the heartbeat is consumed without growing the LLM context.

## Sub-concepts

- [Agent Runtime](agent-runtime/agent-runtime.md)
- [Cross Cutting](cross-cutting.md)
- [Db](../db/docs/db.md)
- [Env Vars](env-vars.md)
- [Infra](infra.md)
- [Runtime Ownership](../ownership/README.md)
- [Lifecycle](lifecycle.md)
- [Loop](loop.md)
- [Mcp Daemon](mcp-daemon.md)
- [Messages](../messages/docs/messages.md)
- [Observe](observe.md)
- [Process Lifecycle](process-lifecycle/process-lifecycle.md)
- [Startup](../startup/docs/startup.md)
- [State](state.md)
- [State Checkpoint Storage](state-checkpoint-storage.md)
- [Sessions](sessions.md)
- [Agent Graph](../graph/docs/graph.md)
- [Hooks](../hooks/docs/hooks.md)
