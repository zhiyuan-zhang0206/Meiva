# Which models a harness runs

Split out of [[base/agents/docs/harness/harness.md|Agent harness
identity]], which owns the value set, its immutability and the fork rule. This
node owns how model and reasoning-effort choices are discovered, validated and
executed per harness.

`meiva` runs every spawnable model declared in the native `ModelCatalog` through
its configured provider API keys (`GET /api/models`). External harnesses
(`claude_code`, `codex`, `antigravity`) own their own
account-specific and CLI-version-dependent catalogues: `harness_probe` queries
each signed-in CLI in real time (`base/agents/birth/harness_model_probe.py`,
`base/agents/model/harness_models.py`) and publishes `models`
(`ProbedHarnessModel(id, reasoning_effort_options, reasoning_effort_default)`)
and `default_model` on the machine's harness availability row. Static model
registry entries carry no external harness tables, and uncatalogued CLI models
(such as `gpt-oss-120b` in `agy` or CLI-only models in `claude` and `codex`)
are selectable and runnable without native catalog registration,
`superseded_by` filtering, or provider API keys.

Each external CLI is probed through a non-agentic protocol query under a bounded
timeout without starting a turn or spending model quota:

- `codex`: `codex app-server --listen stdio://` using the same
  transport and handshake as the session bridge. The probe awaits the
  `initialize` reply before sending `initialized` and keeps stdin open until
  the `model/list` reply arrives. It reads visible `id`, `supportedReasoningEfforts` (including `ultra` when the
  installed CLI and account support it), `defaultReasoningEffort`, and
  `isDefault`.
- `claude_code`: `claude -p --input-format stream-json --output-format stream-json --verbose --no-session-persistence`
  (`control_request` `initialize`), reading `resolvedModel` / `value`,
  `supportsEffort`, `supportedEffortLevels`, and `defaultEffort`. An entry
  whose selector carries `[1m]` contributes two IDs sharing its effort ladder:
  the exact selector, so a selection keeps its 1M-token context window when
  passed back to Claude, and the model behind it (`resolvedModel` with any
  `[1m]` stripped), which the CLI also runs — Claude Code lists Fable only
  through `claude-fable-5-2[1m]`, yet `--model claude-fable-5-2` runs.
- `antigravity`: `agy models`, reusing stdout from the login check, stripping
  progress output and grouping `<base>-<effort>` suffixes into canonical effort
  ladders while preserving effort-less model IDs verbatim.

If a signed-in external CLI fails its model probe or returns zero models, its
probe row degrades to `status="unknown"` with the failure reason and
`launchable=False`. When a caller spawns or switches onto an external harness on
a machine whose gateway `HarnessProbeCache` is cold, the gateway coalesces an
on-demand `harness_probe` (`ensure_machine_harnesses`) and validates
`(llm_model, reasoning_effort)` against the live `ProbedHarnessModel` list via
`resolve_probed_harness_selection` before creating or restarting the agent.
Unsupported models or efforts fail fast with `InvalidModelConfig` (HTTP 400);
omitted fields resolve to the probed `default_model` and its
`reasoning_effort_default` (never the native cluster default or prior-harness
pins).

The stored `llm_model` and `reasoning_effort` select the execution session.
The runner passes them through `bridge_for` and `open_session`: Claude receives
SDK model/effort options; Codex receives them on every `turn/start`, including
after thread resume; Antigravity resolves `<model>-<effort>`
(`resolve_antigravity_cli_model`) onto `--model`. External agents use the CLI's
authentication and do not construct a native chat model or require its provider
API key.

New external births stamp `harness_model_selection: 1` in `birth_config` along
with the probed default `llm_model` and `reasoning_effort` when not overridden.
Only version `1` opts those pins into CLI execution; older rows without the
stamp retain the adapter/CLI's original defaults on resume, and unknown
selection versions fail explicitly.

For usage billing and non-`meiva` context-window reads (`token-usage`,
`context-breakdown`), `resolve_harness_catalog_model` preserves exact catalog
entries verbatim (for example `qwen3.8-max`), unconditionally strips trailing
`[1m]` and unambiguous reasoning-effort suffixes (`minimal`, `low`, `medium`,
`high`, `xhigh`) even when a newly released model is not yet in the target
catalog, and strips `-max` / `-ultra` only when the resulting stem exists in
the target catalog (`catalog.prices` or `catalog.models`). This prevents
uncatalogued external-harness models from splitting ledger rows by effort suffix.
