---
type: doc
title: Sealed source releases
description: A commit staged as a sealed release directory without .git, its manifest, and the source pointer that selects it.
tags: [base, deploy]
---

# Sealed source releases

`base/deploy/sealed_release/` stages one commit as a release directory of a home,
seals it, records it in a manifest and points the home's `source` at it. The plan,
the failures it prevents and the phases still to come are in
[the release-directory plan](../../../../future/infra/lifecycle/release-dir-atomic-code-swap.md).
Production still runs `source` as a Git checkout; nothing switches a home to a release
automatically yet.

## Layout

```
$MEIVA_HOME/source-releases/<code_version>-<sha12>/                  the release tree, no .git
$MEIVA_HOME/source-releases/<code_version>-<sha12>.manifest.json     its manifest, outside the tree
$MEIVA_HOME/source -> source-releases/<code_version>-<sha12>          a relative pointer
```

The tree holds `git archive` of the commit, `.meiva-release.json` (commit, first-parent
count, UTC commit time; its format is owned by `base/native_process/loaded_commit.py`),
the release's own `.venv` built at its final path, and compiled bytecode for every source
file, because a sealed tree cannot take a `__pycache__` later.

## Modules

- `stage_plan.py` (strict): the layout, a release's name, whether a stage reuses, rebuilds
  or builds, and why a switch is refused.
- `build_plan.py` (strict): the record's UTC commit time, the build environment without an
  inherited `VIRTUAL_ENV`, and the `uv sync` and `compileall` commands.
- `sealed_plan.py` (strict): the manifest entry, its JSON form with fail-fast parsing, and
  the differences between a manifest and an observed tree.
- `sealed_tree.py`: observe a tree with `lstat` (and optionally its bytes), seal and
  unseal through `chmod -R` and, on macOS, `chflags -R uchg`, write and read the manifest,
  verify, remove.
- `stage.py`: `stage`, `switch` and the `python -m base.deploy.sealed_release.stage`
  entry (`stage --repo --commit --home`, `switch --home --release`, `verify --release
  [--content]`).

## Invariants

- **Sealed means no write bit anywhere in the tree, and on macOS the immutable flag.** A
  careless write, `uv sync` into the release or `rm -rf` fails. The operator's uid can still
  unseal; sealing contains accidents and verification finds the rest.
- **The manifest is taken after sealing.** It records kind, mode, size, inode, `mtime_ns`,
  `ctime_ns`, symlink target and file SHA-256 per path. The kernel moves `ctime` on every
  write, `chmod` or flag change, so comparing `lstat` alone finds any change; the content
  comparison rehashes every file and is for sealing and on-demand checks.
- **Symlinks are recorded, never followed**: `.venv/bin/python` points at the uv
  interpreter, which is not the release's to seal.
- **A stage is safe to rerun.** A release with a manifest that still verifies is reused (and
  its manifest sealed again); one that changed is refused; a tree without a manifest, or a
  manifest without a tree, stopped part-way and is rebuilt, unless the home's `source` points
  at it. `remove` deletes the manifest first for the same reason.
- **A switch replaces only a pointer**, with one rename, to a sealed release of the same home
  that verifies. A `source` that is a real directory is moved aside by hand (the one-time
  cutover).
- Every release file is its own inode: uv is told the link mode (`clone` on macOS, `copy` on
  Linux) instead of inheriting one. A hardlink into uv's cache would be sealed with the release
  and move its `ctime` when another link changes; a symlink would leave the file unsealed.
- A stage refuses a shallow clone, whose first-parent count would be too low and is fixed
  into an immutable record.
- `switch` is for a scratch home in phase 1: a production home still needs its checkout for
  the fleet update, the drift reads and the frontend build.

## Consumers

- `base/native_process/loaded_commit.py` and `code_version.py` read the release record, so
  a process loaded from a release knows its commit and code version without Git.
- `base/deploy/git/source_tree_guard.py` (health-probe check 8) verifies the manifest when
  `source` points into the home's `source-releases/`.
