# Skill recall

The `# Capabilities` index asks the agent to match every task against it by hand, and an index of hundreds of skills is easy to skim past. Skill recall is the push side: before a turn woken by new inbound, one decision-model request scores every skill against the recent conversation and names the best matches in one system note (`NoteTag.SKILL_RECALL`). It is a sibling of passive memory recall ([Memory Recall — Passive Memory Recall](../../../meiva_builtins/plugins/meiva_memory/docs/memory-recall.md)) with its own switch, model, deadline and record.

## When it fires

`agent/graph/hooks/skill_recall.py:SkillRecallHook` is a framework `before_llm` hook, registered after compact's reminder and before the capability-index drift check (drift stays last). It fires when the messages added since the last assistant turn include an inbound from a **person** (`user`) or a **schedule** (`schedule:<id>`); `agent/messages/__init__.py:tail_has_skill_recall_inbound` is that gate. With `skill_recall_peer_messages` on (on by default), a peer agent's message also fires, after the peer gate below. A system notice, a watcher or a shell completion never triggers it, and memory recall's wider gate is untouched. It also stays quiet in an eval-isolated run and on a native-loop turn where auto-compact is predicted (the note would be wiped with the history).

## What is asked

`agent/graph/recall/skill_recall.py` sends **one** `decide()` request (`usage_kind="skill_recall"`, billed to the agent):

- **State** (sent once): a fixed preamble plus the recent conversation, built by `agent/graph/recall/conversation_query.py:build_conversation_query` (the last 6 real dialogue messages, at most 2000 characters), which is also the query memory recall searches with.
- **Questions** (one `YesNo` per candidate, keyed by the skill's display identifier): "could loading this skill help the agent handle, scope, execute, or verify the latest request?" with the skill's index-line description. The wording lives in `skill_recall.py` and favors recall over stingy filtering — an extra one-line suggestion costs ~35 tokens and can be ignored, whereas missing an applicable workflow, goal-contract, or verification skill causes silent rework. The offered note spells each skill as its loadable path (`meiva.skills.meiva_guide.mcp`) with its `SKILL.md` file path.
- **Candidates**: `agent/graph/prompt/capabilities.py:recall_candidates`: everything the capability index covers, described by exactly the text its index line quotes (`_one_line`, at most 300 characters). A skill that `skills_to_expand_at_start` preloaded is not a candidate. Read and already-offered skills are removed by the hook.

The current `skill_recall_model` default is `gemini-decision-1`. `skill_recall_threshold` defaults to `0.35` (person/schedule turns), `skill_recall_peer_threshold` to `0.45`, and `skill_recall_max` to `5` (calibrated 2026-11-18 on `gemini-decision-1` across real cluster prompts so domain skills do not crowd out workflow and verification skills). Explicit model and threshold pins remain authoritative.

## From scores to a note

Skills whose probability of "yes" reaches the threshold are ranked best first, with the identifier breaking ties (the model's probabilities are coarse, so ties are common), and the top `skill_recall_max` are named in one note in the index's own line shape (including each skill's `SKILL.md` path).

## The per-window record

A skill is not offered if its body is already in the context window or it was already offered. Two core channels hold that, both cleared by a compaction:

- `skill_reads` (`operator.add` list): the exec child appends the identifier of every skill body it hands over. The one point the recorded body reads pass through is `meiva/skills/__init__.py:_record_skill_invoked` (a `meiva.files.read` of a SKILL.md ends there; `meiva.help` shows only name, description and path, and a read by shell `cat` or a harness's own file tool is not noted), which also notes it in `meiva.state_update["skill_reads"]`. Plain strings only: the child imports no state class, no model stack and no network client for this, and `skill_reads` is in `SDK_WRITTEN_BASE_FIELDS` so the exec node accepts the key.
- `skill_recall` (`SkillRecallState`: `prompted` and `window`, last-value, written only by the hook): the skills a recall note named, and the `compact.version` they belong to.

When `compact.version` is past `window` the hook clears both (`Overwrite([])` and a fresh record) on the first pass after the compaction, whether or not it fires: an exec can only follow an LLM call, which this hook precedes, so no read of the new window is lost to the reset. This is the lazy re-arm the SDK reminder uses.

## Failure semantics

The hook runs before every inbound turn's LLM call, so nothing it does may kill the turn. The whole pass runs under `skill_recall_deadline_seconds` (6.0; independent of `memory_recall_deadline_seconds`); a timeout, any `DecisionError` (provider failure, missing key, unknown model) and a `DecisionInputTooLargeError` are logged under `event=skill_recall` and the turn proceeds with no note. The request grows with the catalog (about 150 tokens per question), with an explicit Jev pin, roughly 400 skills reach its 64K request limit; `decide()` then raises `DecisionInputTooLargeError` before sending anything. The state is never truncated and the candidate list is never chunked or sampled: a partial score would offer the best of an arbitrary subset. Gemini additionally caps a request at 1,000 questions; its adapter raises the same typed capacity error before any HTTP request. Such an agent gets no recall until its catalog shrinks or the limit grows.

## Configuration

`AgentSkillRecallSettings` (`base/config/domains/agent/skill_recall.py`, the `SkillRecall` slice in `base/host/env/agent_slices.py`, all per-agent and live): `skill_recall_enabled` (on), `skill_recall_model`, `skill_recall_threshold` (0.35), `skill_recall_peer_threshold` (the peer path's own cut, 0.45), `skill_recall_max` (5), `skill_recall_deadline_seconds` (6.0), plus the peer path: `skill_recall_peer_messages`, `skill_recall_peer_gate_work_floor` (0.30), `skill_recall_peer_gate_max_chars` (4000), `skill_recall_peer_gate_deadline_seconds` (6.0). Both deadlines were raised from 3.0 to 6.0 on 2026-11-18: at 3.0, 21.7% of peer-gate calls (178 of 819 in 24h) timed out on `gemini-decision-1` while the completed ones ran p95 1.67s, so the cut was dropping the model's tail, not hung calls; the field descriptions carry the measurements. An overlay naming `skill_recall_model` is checked against the catalog's **decision** table (`validate_config_overlay(..., decision_models=...)`); a chat model id is rejected.

## Peer messages

A peer agent's message is scored only after one cheap single-question gate (`peer_gate_passes`, `usage_kind="skill_recall_peer_gate"`) over that one message: does it ask the recipient to do concrete work. It skips when work is at or below `skill_recall_peer_gate_work_floor` (0.30); uncertain messages above that floor pass, because a missed offer costs a redo and an extra offer costs one line. Delegated briefs that state steps or acceptance criteria still pass so workers receive relevant domain, workflow, and verification skills. A failed gate call skips the turn — the behaviour before peer messages existed.

Each gate decision logs one `skill_recall` event with `stage="peer_gate"`, `outcome` (`passed`, `skipped` or `failed`) and `latency_ms`; a pass or skip adds `work`, a failure adds `reason` (the innermost exception class, such as `ReadTimeout` or `ConnectTimeout`) and stays at warning. These are declared payload keys, so the OTLP export turns `latency_ms` into the `meiva_skill_recall_latency` histogram labelled by `stage` and `outcome`, which is where the gate's success rate and latency are read. `telemetry_events` does not hold the info-level lines: `skill_recall` has no Postgres reader, so it is not `persist`ed ([telemetry_events stores what is read](../../../docs/decisions/observability/telemetry/2026-10-03-telemetry-events-stores-what-is-read.md)) and only the warning-level failures land there by their level. The JSONL mirror and Loki keep every line; in Postgres the successful calls are the `llm_usage` rows with `usage_kind="skill_recall_peer_gate"`.

Replay of 999 peer messages (2026-10-18, jev-1.13.0): the single work question scores 71.5% precision at **96% recall** (the former second question checking whether the brief already stated a method dropped recall to 78.3% and precision to 69.9%, and was removed on 2026-11-18).

A turn the gate lets through is scored against `skill_recall_peer_threshold` (0.45), stricter than the person cut (`0.35`) to account for residual peer coordination traffic. Re-thresholding the historical 2026-10-18 `jev-1.13.0` 999-message fixture (`peer_recall_replay.json`) from `0.70` to `0.45` raises work-request coverage on those recorded scores from 63.3% (`271/428`) to 98.6% (`422/428`).

Parent: [Graph](graph.md).
