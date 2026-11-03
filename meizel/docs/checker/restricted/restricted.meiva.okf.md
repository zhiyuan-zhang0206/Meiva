---
type: doc
title: "meizel/ — Restricted Python"
description: "Dynamic imports, exec/compile, string patches, sys.modules writes, Python subprocesses, attribute names only the run knows, and the [loads], [external] and [dynamic-attrs] declarations."
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

Three more forms leave a dependency to the run instead of the source, all reported as
`restricted-python`:

- **dynamic attribute name**: the builtins `getattr`, `hasattr`, `setattr` or `delattr` (found
  through aliases and `builtins.getattr`; a local `def getattr` is not the builtin) whose second
  argument is not a string the source states. A literal, a literal sum, a name bound once to a
  literal in the same file, and a name imported from a first-party module that binds it once to a
  literal (followed through re-exports) are stated; a parameter, a loop variable, an f-string, a
  conditional and `getattr(*args)` are not. The builtin handed to another call is judged the same
  way: in `object_of(getattr, target, name)` the arguments after `getattr` are the object and the
  name, so `name` must be stated (`functools.partial(getattr, target)` gives no name yet and is
  not judged).
- **module `__getattr__`**: a module-level `def __getattr__` (PEP 562), the module that answers for
  names it does not bind.
- **class `__getattr__`**: a `__getattr__` or `__getattribute__` defined in a class body, the class
  that forwards the attributes it lacks.

`vars(x)[name]` and `x.__dict__[name]` are not yet judged. A name given to the other forms
(`__setattr__`, `operator.attrgetter`) is not judged either.

## `[loads]`, `[external]` and `[dynamic-attrs]`

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

A file that owns an attribute name only the running program knows declares it under
`[dynamic-attrs]` with a reason:

```toml
[dynamic-attrs]
"proxy.py" = "forwards every attribute to the connection it builds on first use"
```

The file is then exempt from the three attribute forms above and from no other; no edge is made.
The reason names the set of names the file reflects on and where that set comes from (a plugin
declaration, operator configuration, the wrapped object). An entry whose file has none of the
three forms is `stale-dynamic-attrs`. Like `[external]`, a site exists only where
`meizel.policy.toml` lists it.

The table is for names that are open: a transparent proxy, a namespace assembled from plugins,
a name the operator or the user writes. It is not a way to carry a debt. A loop over the
fields of a dataclass, a pydantic model or an argparse namespace, and a lazy re-export of a few
names written in the source, name a fixed set: read the fields by explicit access
(`dataclasses.asdict`, `model_dump`, `vars(args)`, `ast.iter_child_nodes`) or record the sites
under `[debt.restricted-python]` until someone does.

Parent: [[meizel/docs/checker/checker.meiva.okf.md|the checker]].
