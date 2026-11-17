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
   `69.9%` on the historical `jev-1.13.0` 999-message benchmark), while the
   `1.5s` peer-gate deadline logged `70` `ReadTimeout` skips in 24 hours.

`skill_recall.py` now uses a recall-first question ("could loading this skill
help the agent handle, scope, execute, or verify the latest request?"), lowers
`skill_recall_threshold` to `0.35` and `skill_recall_peer_threshold` to `0.45`,
raises `skill_recall_max` to `5` (bounded at ~180 tokens per note while leaving
headroom alongside domain skills), aligns `skill_recall_peer_gate_deadline_seconds`
to `3.0s`, and removes `PEER_GATE_METHOD` and
`skill_recall_peer_gate_method_ceiling` outright so `peer_gate_passes` screens
only `PEER_GATE_WORK`.

Offline scoring and selection replay (`decide(..., timeout=10.0)`, measuring
scoring and top-k selection rather than production `3.0s`-deadline arrival rate)
over 18 representative cluster prompts against the 79-skill catalog on live
`gemini-decision-1` showed:
- Across the 10 multi-step delivery prompts (`Bucket A`), family-any selection
  (`meiva-workflow`, `meiva-goal`, or `meiva-workflow:define-goal` in the
  offered note) rose from `4/10` (`meiva-workflow` `3/10`, `meiva-goal` `0/10`,
  `define-goal` `1/10`, with `4/10` returning empty `[]`) to `9/10`
  (`meiva-workflow` `9/10`, `meiva-goal` `7/10`, `define-goal` `4/10`, `0/10`
  empty).
- Across the 5 non-goal control prompts (`C_philosophy`, `D_markdown_rule`,
  `D_disk_clean`, `D_status_ping`, `D_model_switch`), `define-goal` was
  selected `0/5`; on the 4 non-cleanup controls (`C_philosophy`,
  `D_markdown_rule`, `D_status_ping`, `D_model_switch`), `meiva-goal` was
  `0/4` (`C_philosophy` included `meiva-workflow` at rank 4), while
  `D_disk_clean` ranked `meiva-guide:workspace-cleanup` first (`0.975`) with
  `meiva-goal` (`0.827`) and `meiva-workflow` (`0.604`) as benign secondary
  offers.
- Re-thresholding the historical `2026-10-18` `jev-1.13.0` 999-message peer
  score fixture (`peer_recall_replay.json`) from `0.70` to `0.45` raises
  work-request coverage on that fixture from `63.3%` (`271/428`) to `98.6%`
  (`422/428`); live `gemini-decision-1` end-to-end peer-gate + recall latency
  and precision remain observed via production `skill_recall` telemetry rather
  than established by that historical fixture.

Targeted verification covers `agent/graph/hooks/tests/test_skill_recall_hook.py`,
`agent/graph/hooks/tests/test_skill_recall_peer_threshold.py`,
`agent/graph/hooks/tests/test_skill_recall_question_ids.py`,
`agent/graph/hooks/tests/test_skill_recall_capacity.py`, and `base/tests/config/`.
