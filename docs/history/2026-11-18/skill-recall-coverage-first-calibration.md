# Recall-first skill recall calibration and single-question peer gate

Seven days of cluster `audit` telemetry (`253` `skill_invoked` events across
`40,601` audit rows) recorded zero spontaneous loads of `meiva-goal` and
`meiva-workflow:define-goal`, even though real user-input sampling (`143`
messages across `39` episodes) showed over half of engineering, product, and
research episodes belonged to multi-step verifiable deliveries. Three
precision-oriented filters in `agent/graph/recall/skill_recall.py` and
`base/config/domains/agent/skill_recall.py` compounded to suppress workflow and
verification playbooks:

1. `QUESTION_TEMPLATE`, `TRUE_IF`, and `FALSE_IF` required a skill to "clearly"
   and "directly" help and answered `No` whenever "the agent can handle the
   latest request just as well without it" — causing `gemini-decision-1` to
   reject meta-workflow, goal-contract, and verification skills whenever the
   user stated a concrete domain request without naming `"goal"`.
2. `skill_recall_threshold` (`0.60` person / `0.70` peer) combined with
   `skill_recall_max = 2` truncated workflow and goal skills whenever two domain
   skills also matched (or tied alphabetically).
3. `PEER_GATE_METHOD` (`skill_recall_peer_gate_method_ceiling`) rejected
   delegated peer briefs that included steps or acceptance criteria, cutting
   peer gate recall from `96%` to `78.3%` (and precision from `71.5%` to
   `69.9%` on the 999-message benchmark), while the `1.5s` peer-gate deadline
   logged `70` `ReadTimeout` skips in 24 hours.

`skill_recall.py` now uses a recall-first question ("could loading this skill
help the agent handle, scope, execute, or verify the latest request?"), lowers
`skill_recall_threshold` to `0.35` and `skill_recall_peer_threshold` to `0.45`,
raises `skill_recall_max` to `5` (bounded at ~180 tokens per note while leaving
headroom alongside domain skills), aligns `skill_recall_peer_gate_deadline_seconds`
to `3.0s`, and removes `PEER_GATE_METHOD` and
`skill_recall_peer_gate_method_ceiling` outright so `peer_gate_passes` screens
only `PEER_GATE_WORK`.

Replaying 18 representative cluster prompts against the 79-skill catalog on live
`gemini-decision-1` raised `meiva-workflow` / `meiva-goal` / `define-goal`
recall on multi-step delivery tasks from `0/10` (`meiva-goal`) and `1/10`
(`define-goal`) — with `4/10` returning empty `[]` — to `9/10`, while keeping
`define-goal` false positives at `0/4` across open philosophical dialogue and
atomic configuration/status commands. Targeted verification covers
`agent/graph/hooks/tests/test_skill_recall_hook.py`,
`agent/graph/hooks/tests/test_skill_recall_peer_threshold.py`,
`agent/graph/hooks/tests/test_skill_recall_question_ids.py`,
`agent/graph/hooks/tests/test_skill_recall_capacity.py`, and `base/tests/config/`.
