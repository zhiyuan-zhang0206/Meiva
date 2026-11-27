# scripts/rules/ — This Repository's Own Rules

## What is it

The rules here are this repository's, not the checker's. The scripts under `scripts/lint/` and
`scripts/structure/` run them, and `python -m meizel check` does not. They use the installed
[meizel](https://github.com/zhiyuan-zhang0206/meizel) as a library: its source tree
(`meizel.graph.universe.SourceTree`), its lint vocabulary (`meizel.lint_common`), its roles and its
policy reader. They read their exemptions from tables in `meizel.policy.toml`, and the
`[sections]` table there declares the shape of each table (see the checker's policy document), so
`policy diff` and the policy-removal gate review them the same way as the checker's own tables.

## Runtime boundaries

The structure gate checks meizel's resolved imports. Claim execution composes
its timeline through `ReadTimelineSink`. Completed-record settlement uses
record location helpers. Quote admission consumes canonical source identities.
The legacy cursor module owns the remaining display-based lookup.

Gateway and claim request paths stage external accounting through service-owned
receipts. The direct native provider emitters and the receipt delivery worker
hold the row-write capability. The database adapter owns ledger appends and
replay. The repricing service owns historical price updates. Raw ledger
mutations and the row-write capability have explicit owners in `[single-owner]`.

The authorized bootstrap mirror and enrollment-marker filenames belong to the
bootstrap module. Other modules use its mirror APIs, which validate the signed,
host-bound envelope before staging.

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
  - `env_presence.py` judges an environment variable by its value, never by its presence:
    `"X" in os.environ`, `os.getenv("X") is None`, a `KeyError` handler around `os.environ["X"]`
    and a shell `[ -v X ]` all read an optional secret that is exported empty as configured. Its CLI
    (`env_presence_cli.py`) judges production Python, shell scripts and the workflow and action
    files, and its exemptions are under `[env-presence] files`.
    What it does not cover: `os.environ.get(NAME, default)` (and `os.getenv(NAME, default)`) keeps an
    empty value instead of the default, so it is the same mistake in a shape the rule cannot tell from
    a deliberate default; write `os.environ.get(NAME) or default`. In a shell, `${X-default}` and
    `${X+x}` test presence the same way; use `${X:-default}` and `${X:+x}`.

  Both config rules are effect shells. The `*_plan.py` modules decide what is scanned and what a
  finding says, and `lint_paths.py` owns the two decisions they share. Their launchers in
  `scripts/lint/` bind the application's config registry.
- `language/`:
  - `path_imports.py`: code under `meiva_builtins/` reaches other code through packages, never
    through a file path.
  - the PYTHONSAFEPATH sibling-import guard: `no_script_sibling_imports.py` is the shell, and
    `script_siblings.py` makes the decisions.

Each rule group keeps its tests in its own `tests/` directory.
