---
type: doc
title: "meizel/ — Remaining Host-Specific Knowledge"
description: "The places under meizel/ that still name this repository's paths, fixtures, roles or vocabulary instead of reading them from the policy or a manifest: the work list for the next decoupling change."
tags:
  - locality
  - tool
---

# meizel/ — Remaining Host-Specific Knowledge

Parent: [[meizel/docs/meizel.meiva.okf.md|the meizel node]].

The code under `meizel/` is not yet free of this repository: the places below still name its paths,
fixtures, roles or vocabulary. This is the work list for the next decoupling change, not a statement
of what is acceptable; each entry should move to the policy, to a manifest, or to the host's own
tooling. Paths are non-test code and lines are those of the commit that wrote the list.

- Test selection (`meizel/test_graph/`):
  - `impact.py:24-37` `TREE_SCAN_PINS`, eleven Meiva test paths; `impact.py:40-49`
    `_GLOBAL_NAMES` (`uv.lock`, `.github/workflows/ci.yml`); `impact.py:161` `schedules`,
    `impact.py:170` `ui/` and `impact.py:179` `.github/actions/`; `impact.py:259` and `:273`,
    `checkout_plan.py:28` and `:89`, `meizel/checkout.py:710` and `:715`: the `tests/e2e/` layout.
  - `lanes.py:30-48` fixture names of the host's database and bus (`gateway_data_plane`,
    `cluster_defaults_unset`, `aops_pool`, `aredis_inbound_listener`, `postgres_access`,
    `redis_access`, ...), `lanes.py:49` `NATIVE_MARKERS` (`native_permissions_helper`) and
    `lanes.py:51-57` `NATIVE_MODULES`.
- Contracts: `contracts/enumerations.py:190` `_outside_rule` (`meizel/`, `docs/`);
  `contracts/ambient_read.py:50` `_GOVERNED_ROLES`, the host's role vocabulary.
- Single decision owners (`owners/`): `decisions.py:33-54` and `:150-161` (the `postgres-dial`
  decision, with the `psycopg` entry points it recognises); `timing_judgement_baseline.json`, a
  baseline of the host's own test files.
- Rules over the host's conventions (`state/`): `no_os_environ.py:2-5`, `:48` and `:75` and
  `no_os_environ_plan.py` (the `Settings` singleton and its config registry);
  `turn_scoped_config.py` and `turn_scoped_config_plan.py` (the `settings.<a>.<b>` form);
  `machine_role.py:4` and `:34` (`machine_role()` and `MachineIdentity`);
  `ambient_state/allowlist.py:56` and `:78` (`settings_names`), `ambient_state/__init__.py:117`
  and `ambient_state/scan.py:23` and `:168-194` (the `settings` vocabulary in the text and the
  detection).
- Paths of the host's tooling: `lint_common.py:74-82` (`scripts/lint/`, `scripts/structure/`, ...);
  `language/purity.py:72` (`scripts/<top>.py`); `language/python_commands.py:8` and
  `language/path_imports.py:37`; `graph/python_layout.py:176-183` (the standalone-project CI
  contract: `uv.lock`, a `cpu` extra).
- Documentation paths in diagnostics: `budgets/budget_plans.py:53`, `language/path_imports.py:37`,
  `language/scan.py:69` and `owners/decisions.py:132` point at
  `docs/conventions/python-conventions.md`.
- Toolchain conventions, a weaker coupling: `source_evidence/loader.py:25`, `:26` and `:69` read
  `pnpm-lock.yaml`.
