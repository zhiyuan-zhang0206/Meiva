---
type: doc
title: "meizel/ — Restricted Python"
description: "Dynamic imports, exec/compile, string patches, sys.modules writes, Python subprocesses, and the [loads] and [external] declarations."
tags:
  - locality
  - tool
---

# meizel/ — Restricted Python

## Restricted forms

Binding resolution finds these through aliases (`from importlib import
import_module as im`): `importlib.import_module`, `__import__`,
`runpy.run_module`, `runpy.run_path`, `importlib.util.spec_from_file_location`,
`exec`, `compile`, a Python subprocess that runs anything but a literal file or module
([[meizel/docs/checker/restricted/python-commands.meiva.okf.md|commands]]), a `pytest_plugins` that is not a literal, string patch
targets that name first-party code (`mock.patch("a.b")`, `monkeypatch.setattr("a.b", v)`;
stdlib and third-party targets are fine, an unreadable target is not), and
writing a first-party module into `sys.modules` by name (`sys.modules["a.b"] = ...`,
`del`, `pop`, `setdefault`, `update`, `monkeypatch.setitem/delitem`, `patch.dict`).
Reading `sys.modules` is not restricted.

## `[loads]` and `[external]`

A file that owns a dynamic load declares it:

```toml
[loads]
"loader.py" = ["meiva_builtins"]   # repository-relative directories
```

Each entry becomes a fact edge from the file to the directory, judged by the
same rule; the file is then exempt from the restricted-form errors. A loading file only loads.

A file that runs code from outside the repository (user scripts, agent code, a
module named by operator configuration) declares it under `[external]` with a reason:

```toml
[external]
"runner.py" = "executes the schedule script the user wrote"
```

The file is then exempt from `exec`, `compile` and the dynamic-import forms; no edge is made. Other restricted forms (`sys.modules` writes, python commands,
string patches) still apply. The reason must be non-empty; an entry whose file
has none of the exempt forms is `stale-external`.

Plugin and schedule-script discovery is `[external]`, not `[loads]`.

Parent: [[meizel/docs/checker/checker.meiva.okf.md|the checker]].
