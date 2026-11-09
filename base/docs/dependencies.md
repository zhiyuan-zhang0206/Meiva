# Base library dependencies

The base-layer domain map below complements the public
[entry points](entry-points.md).

- [Language Model Provider Layer](../lm/docs/lm/lm.md) — LLM provider abstraction layer, used by agent/graph/llm/node.py via factory to build chat models
- [Agent Cross-Process Contract](agents-contract.md) — agent ↔ gateway state/exception/wire protocol contract
- [Message Metadata Contract](../agents/messages/docs/kwargs.md) — typed `meiva_*` metadata inside a message's `additional_kwargs`
- [Inbound Provenance Facts](../agents/messages/docs/inbound-provenance.md) — non-enforcing credential, transport, content-hash, and source-assertion facts on gateway inbounds
- [Logging](../log/docs/log.md) — structured logging, feeds the unified event emitter (`base/telemetry/emitter.py`)
- [Metrics](../telemetry/metrics/docs/metrics.md) — system-level metrics computation core
- [Database Layer](../../agent/db/docs/db.md) — base/db/__init__.py provides database connection pool, depended on by services and gateway
- [Gateway & CLI](../../gateway/docs/gateway-cli.md) — gateway communicates with agent processes via the contracts in base/api_contracts/agent_contract.py
- [Live Event Channel (`meiva:events`)](../events/live/docs/live/live.md) — `meiva:events` live pub/sub payload union
- [Machine Identity & Capabilities](../cluster/machine/docs/machine.md) — machine name + capability set, `machines` table, spawn-target invariant
- [Schema Migrations (baseline + deltas)](../deploy/schema/docs/migrations.md) — baseline + delta schema model, applied set, version assertion
- [`$MEIVA_HOME` Layout](../paths/docs/paths.md) — `$MEIVA_HOME` layout
- [Install Registry (`installed.json`)](../packages/extensions/docs/install_registry/install_registry.md) — `installed.json` + the skill-scanner gate
- [Plugin Enable Config](../packages/plugins/docs/enable_config.md) — per-machine plugin enable state
