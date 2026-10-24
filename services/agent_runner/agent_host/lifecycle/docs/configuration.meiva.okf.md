---
type: doc
title: Host configuration composition
description: Current ownership boundaries and their executable evidence.
---

# Host configuration composition

One host boot provides the host policy as values read once (capacity, cache bounds, model default and override, registration lifetime, stall bounds, checkpoint interval), the default reader and clock factory the base slice and context APIs still take, machine identity, plugin configuration authority and the process home. A changed cluster configuration reaches the host with its next start; the home is handed to the host, which hands it to the termination cleanup. A profile-restricted process defers construction of the all-domain Settings view until the authority needs it. The shared graph retains the installation's registry for the process lifetime; plugin configuration reads can remain live without pretending the graph can change its registry. Logging initialization closes the supplied clients on failure while preserving the primary exception.

The root supplies handoff stamping as a separate required `TimestampSource` resource to `AgentHost`; an admitted activation or new handoff note mints one pure `TimestampPolicy`. A stopped settlement or existing end-note receipt does not observe the clock. It is not a field in `HostPolicy`. The existing default reader and clock factory remain legacy collaborators, so the whole host policy is not a pure configuration value.

`stored_config.py` owns the per-turn `StoredConfig` row and its configuration fingerprint. Wake screening reads the row without importing runtime construction, model wiring or agent process boot. The host still re-reads the database row each turn; a changed fingerprint rebuilds its cached runtime. This import boundary does not change the configuration snapshot contract: the row remains a mutable dataclass and retains its supplied maps and nested references. Admission normalizes the separately resolved pins rather than rewriting those stored maps.

The fingerprint keeps overlay and birth maps in separate slots, distinguishes absent maps from empty maps, sorts JSON keys and preserves `default=str` coercion for non-JSON decoded scalars. It is evaluated from the row's current maps on each access. The mutable row is not a deeply frozen configuration value.
