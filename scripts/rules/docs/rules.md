# scripts/rules/ — This Repository's Own Rules

## What is it

The rules here are this repository's, not the checker's. The scripts under `scripts/lint/` and
`scripts/structure/` run them, and `python -m meizel check` does not. They use the installed
[meizel](https://github.com/zhiyuan-zhang0206/meizel) as a library: its source tree
(`meizel.graph.universe.SourceTree`), its lint vocabulary (`meizel.lint_common`), its roles and its
policy reader. They read their exemptions from tables in `meizel.policy.toml`, and the
`[sections]` table there declares the shape of each table (see the checker's policy document), so
`policy diff` and the policy-removal gate review them the same way as the checker's own tables.

## Contents

- `owners/`: one owner per external capability. `decisions.py` holds Rule 5: each design
  decision has exactly one owning module, every other site is a bypass, and an exemption that
  goes stale fails. The owners and the exemptions are under `[single-owner]`. `clock_lattice.py`
  keeps lattice-vocabulary timing constants inside the clock-lattice family modules, or as
  aliases of a registered clock. `timing_judgement.py` reports wall-clock comparisons in test
  assertions ([the timing judgement](timing-judgement.md)); its baseline is
  `meizel.timing_baseline.json` at the root.
- `state/`: dependency-injection rules.
  - `ambient_state/` is Rule 9: module-level state read to make a decision, and free-floating
    background work ([ambient state](ambient-state.md)).
  - `machine_role.py` is Rule 3, the retired ambient machine-role reader.
  - `no_os_environ.py` stops a test from setting a variable that `Settings` models.
  - `turn_scoped_config.py` forbids per-agent config through a process global.

  Both config rules are effect shells. The `*_plan.py` modules decide what is scanned and what a
  finding says, and `lint_paths.py` owns the two decisions they share. Their launchers in
  `scripts/lint/` bind the application's config registry.
- `language/`:
  - `path_imports.py`: code under `meiva_builtins/` reaches other code through packages, never
    through a file path.
  - the PYTHONSAFEPATH sibling-import guard: `no_script_sibling_imports.py` is the shell, and
    `script_siblings.py` makes the decisions.

Each rule group keeps its tests in its own `tests/` directory.
