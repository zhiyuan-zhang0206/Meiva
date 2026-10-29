---
type: doc
title: Configuration Boot and Catalog Projections
description: Ownership of test configuration, session boot and schema-owned plugin delivery.
tags: [evaluation, quality-assurance]
---

# Configuration Boot and Catalog Projections

Root fixture roster: [[test-fixtures.meiva.okf.md]].

## Requested configuration

- `process_config` snapshots a session `ConfigBoot` only when a consumer requests configuration, before its function home. Provisioning publishes requested URLs to that owned value. `configuration.snapshot_process_config()` preserves explicit/default origins in an independent, one-time snapshot; later changes belong on that owner, not legacy settings. SDK consumers bind `meiva.tests.fixtures.sdk` locally for context and recorder isolation. SDK operation tests opt into `sdk_model_owner`; installer preconditions and settings stay separate.

## Session boot and delivered plugin policy

For configuration consumers, `env_bootstrap._config_chain_upgraded` upgrades
boot before the first function-scoped leak snapshot. Otherwise a first test
could add Settings, then leak restoration remove its names while leaving boot's
upgraded latch set.

The session-wide `provisioning._catalog_cluster_projection` owns plugin-policy
delivery. It activates only after configuration or a model catalog was imported;
other collections remain settings-free. The suite simulates an enrolled runner,
so this owner delivers a real schema-owned projection from its private home
with `plugin_bootstrap_config`. `skip` never grants local authority. It restores
the previous carrier at session teardown. Catalog/authority subprocess tests
receive explicit homes, roles and projections, including missing/invalid packets.
Fleet fixture overrides start from the same complete schema-owned packet and
replace only their Fleet values, preserving other plugins' cluster policy.
Launched SDK probes also generate that complete packet before importing the SDK.

Collection precedes session fixture setup. The provider roster matrix therefore
reads its real catalog in one bounded child with a bare scratch home and an
explicit environment. Pytest still expands and reports every model separately;
collection neither installs parent-process policy nor loads its Settings.


## Import isolation

- **`env_bootstrap.py` stays first in root `pytest_plugins`, with environment isolation before every project import.** Configuration boot force-delivers its home's `.env`; importing first would capture the operator's production home and replace test sentinels. `_assert_env_precedes_project_imports()` rejects early imports; `tests/harness/test_home_isolation.py` verifies the result independently.
