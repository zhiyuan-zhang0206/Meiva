---
name: meiva-fleet
description: "Coordinates Meiva agents through assignments, peer messages, and handoffs. Use when delegating work, reporting progress, or handling blockers."
---

# Working in a Fleet

Fleet is one optional implementation of collaboration. Use `meiva-workflow` to
choose how to work; direct execution, peer cooperation, and script orchestration
remain valid. Read the
[operating contract](reference/operating-contract.md) before using its labels,
questions or delegation; it owns detailed operating obligations.

A **fleet** is a graph of agents working toward a human's goals, supervised from one **fleet view** — a live picture of who is responsible for what, and what each is doing right now. The human steers *decomposition* (how the work is split) and judges *results* (what came back), without opening any agent's conversation. You are one node in that graph: you may direct other agents and be directed by them at any moment, and the relationships that matter are who is talking to whom right now, not any fixed rank.

Agents are general-purpose. Every worker already indexes every skill on its machine, so division of labor comes from what each agent is asked to do. A spawn brief that does not name the skill(s) the worker must use is incomplete; the worker loads each named skill first. (`config_overlay` can still set `skills_to_inject_into_system_prompt`, but under a universal index that only *narrows* a worker's view; it grants nothing.)

## Choosing a collaborator

Proactively parallelize independent work. Do not let a separate piece of work
queue behind your current one: fork when the child needs your context — it inherits
this conversation and starts already oriented — or spawn a fresh agent with a written
brief when it does not. Forking does not shrink your own window, and the child pays
this context's size on every call.
Check existing ownership to avoid duplicate work; a matching role does not by
itself mean the agent can take on more work now. Consult nearby peers when
ownership is unclear, rather than requiring a round of questions before every
spawn. Keep delegated work within the accepted task's scope.

## Coordination and follow-up

Send assignments, blockers and results with `meiva.agents.send_message`. Include
the expected outcome, stage of work and any artifact paths the recipient needs.
Use `meiva.self.terminate(message=...)` to finish and report the result to your
nearest living birth ancestor. Send the delegator a direct report when they are a different agent.

Use schedules for timed follow-up and recurring checks. Track repository defects and
CI failures in GitHub Issues. Keep durable decisions and handoff context in the
workspace's `memory/MEMORY.md`.

## Two Roles

**Orchestrator** (coordinating delegated work) and **worker** (carrying out an accepted mission) are optional descriptions of current work, not exhaustive agent types or fixed identities. An agent may execute directly, collaborate as a peer, or combine these activities without adopting either role.

| Role | What you do | Read |
|---|---|---|
| **Orchestrator** | Decompose a goal, spawn workers with the right skills, supervise them, gather and judge results | [reference/orchestrator.md](reference/orchestrator.md) |
| **Worker** | Own a mission end to end, report results to your delegator, reach the user for their decisions | [reference/worker.md](reference/worker.md) |

## Communication procedure

The Fleet system prompt keeps message-delivery semantics resident. The
[operating contract](reference/operating-contract.md) owns detailed coordination,
and handoff procedures. Use `send_message` for peer delivery and
`meiva.ui.ask` for user decisions.

For each milestone, name its reporter and the agent that must act in the brief.
Save the result in its artifact and send its path with what changed and what is needed.
Combine related findings; other participants send only additional evidence,
blockers, or changed results.

When a requester needs your acceptance or timing to coordinate, communicate
that commitment. Otherwise, act on received messages without a courtesy reply.
An explicit reporting agreement and protocol receipt ACKs still apply.

Apply the core user-attention test before any user-visible delivery. Progress
and conclusions reach the delegator for aggregation; necessary user authorization
or decisions reach the user directly. Having no delegator chooses a direct route
only for information that passes that test; completion and standing responsibilities
do not create an independent reason to publish. Keep other results in working records.
For communication with the user, follow `reduce-context-switch-for-human`.
For mechanical monitoring and waiting, use `meiva-being-a-long-running-agent`
and `meiva.help(meiva.schedules)`; bounded background waits and schedules follow the same fleet contract.

## Two Dials: Effort and Autonomy

Every delegation is set along two independent axes — per task, and (because decomposition is recursive) per reduce point, not once globally.

- **Effort** — how much capability you spend to get a node right, in three fungible currencies: a **stronger model**, a **finer split** (decomposing further — deciding where to cut is itself intelligence), and **harder verification** (a glance, then one adversarial evaluator, then several distinct-perspective evaluators). They trade off: a hard sub-task can buy quality with one strong-model run *or* several cheap runs cross-checked by an adversarial judge.
- **Autonomy** — who consumes the evidence and makes the call. Low: a human decides at every reduce point. High: you decide everything and the human sees only the final result. Independent of Effort — you can pile up a deep multi-lens evaluation and *still* hand the decision to a human (high Effort, low Autonomy), or barely evaluate and run on your own (low Effort, high Autonomy).

A small parallel delegation you eyeball yourself is just **low Effort + low Autonomy**; a background pipeline that decomposes, evaluates adversarially, and converges on its own is **high Effort + high Autonomy**. Same machinery, two dial settings — not different modes.

**Setting Autonomy is not a config flag — it is a message.** When you want the user to make a reduce-point call, you do not write a new state field; you `meiva.ui.ask(...)`; their answer arrives as a message. The user "turning the dial down" on a node is simply you waiting for their decision. No shared mutable state, no schema — the existing message paths *are* the dial.

**High Autonomy is bought with one upfront approval, not many.** Before a batch of autonomous work, package the whole ask into a single `meiva.ui.ask(...)`: the scope and intended work, and the budget ceiling in dollars. The user's reply is the authorization — record it verbatim in the agreed handoff record, so any agent that picks the work up sees exactly what was granted. Once granted you run: no step-by-step check-ins, and whether the user is online or away changes nothing about how you proceed.

## Autonomy's boundaries

Authorization widens what you may do on your own; it does not remove the rails. Inside any autonomous run, whatever the dial:

- **Produce PRs, never merge them** — a human, or a reviewer explicitly delegated the merge, merges.
- **No outward messages** — nothing sent to real people or external services on your own authority.
- **Don't touch prod** — no deploys, no prod data-plane writes, no rollout.
- **Spend only what was authorized** — the budget in the approval is a ceiling, not a target.

Load `meiva-being-a-long-running-agent` for
its agent usage script: choose IDs, a time window, and spawn/fork birth lineage,
then notify the peers responsible for the work when the agreed threshold is
reached. The receiving agent decides how to converge, finish a safe unit,
preserve results, prepare a handoff, or ask for a revised budget.

## Reaching the User vs Another Agent

The **user** is the only one who can authorize what no agent may decide alone — an irreversible or outward-facing action, spending real money, sending something to a real person. This authority is not delegated down the spawn graph: the user is a single queue, reachable directly from any agent.

- **Another agent can decide it** → `send_message` them — you are waiting for their answer.
- **Only the user can decide it** → ask a question with `meiva.ui.ask(...)` — you are waiting for their decision.

## Reporting

Your messages and artifacts record how the work progressed. `meiva.ui.ask` is the question channel: when a question needs a user answer, post it to the user's queue. Ordinary information and results use the configured publish/text delivery protocol. Don't promote routine progress; don't bury a decision that needs an answer.

### Message discipline: milestone-based, no noise

Peer messages are wake-ups: each one costs the recipient a full turn (user
ruling 2026-09-03). So send only when there is a real reason:

- **Send** at a milestone (a stage finished, a decision made, a result
  landed), at a **blocker** (something only the other side can resolve), at
  **completion** (with the delivered result — include the artifact path),
  or for a genuine **request** for something you need.
- **Do not send** routine progress ("still working", "step 3 of 7 done"),
  and **do not send pure acknowledgments** ("ok", "got it"). A message that
  changes nothing for the recipient is a cost with no benefit; if an
  inbound message needs no action and no answer, do not reply.

A delegator decides the exception — "ping me when the branch is pushed" is
a valid line in a brief — but the default is silence between milestones.

Use Ask for questions only the user can answer, in live and asynchronous
conversations. Deliver qualifying information once through the configured
publish/text protocol. Required process narration stays in Details in prefix
mode and unpublished in SDK mode; naive text delivery has no hidden-text prefix.

## Artifacts: File + Message, Never Shared Mutable State

A worker publishes a result by **writing a file and sending the path** to whoever needs it (`send_message`). Siblings consume each other's *published* artifacts (the message they received), never each other's live workspace. There is no shared artifact store and no shared mutable state — every agent owns its own workspace, and a reduce is the only place outputs meet. (Single machine: forks land locally and share the filesystem.)

### Asking one numbered question

For discrete decisions, pass `meiva.ui.ask([{"title": "...", "type": "single_select",
"options": [{"id": "stable-id", "label": "Choice", "description": "Optional detail"}]}])`.
Use `"type": "multi_select"` for multiple selections; an open question is `"type": "text"` (the default). Put the question in `title`
and context in `content`. One call may carry several questions and returns one id each; the user answers all open questions in one reply. Options keep their declared 1-based numbers. Text questions have one answer field; choices keep an optional Note. `note_suggestion` offers candidate text for that field until the user explicitly applies it.
Question/context changes require a replacement question. Interpret both explicit
selections and the user's comment, preserving references such as "option 2";
mentions inside text are not selections. A dismissal is not approval.
