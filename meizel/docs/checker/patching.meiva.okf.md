---
type: doc
title: "meizel/ — Patching"
description: "Every patch is debt: first-party, standard-library, third-party, environment and unresolved targets each have their own code, plus two patch-shaped codes (raising=False and a test subclass that overrides a method); recordable and shrink-only; what to do instead."
tags:
  - locality
  - tool
---

# meizel/ — Patching

## What is it

A patch means the code under test reaches its collaborator through a module or
class attribute, or through the environment, instead of receiving it. The fix is
structural, not a better patch: split the code into a public facade and an
implementation whose dependencies are explicit parameters, and let the test pass
the fake in directly. Meizel reports every patch, by what is patched:

| code | the target |
|---|---|
| `first-party-patch` | a first-party module or object |
| `stdlib-patch` | the standard library (`time.sleep`, `json.dumps`) |
| `third-party-patch` | any other package (`requests.get`) |
| `env-patch` | the environment: `monkeypatch.setenv/delenv`, `setitem/delitem` or `patch.dict` on `os.environ` |
| `unresolved-patch` | a target the file's imports do not resolve: an argument, `self.attr`, a class defined in the same file, a call result |
| `patch-raising-false` | `monkeypatch.setattr` with `raising=False`, whatever it patches |
| `test-subclass-override` | a class in a test that overrides a method of a concrete first-party class |

## What counts

`mock.patch(...)` with a string target, `mock.patch.object`, `mock.patch.multiple`
(one site per patched name), `mock.patch.dict`, and `monkeypatch.setattr`,
`delattr`, `setitem` and `delitem` on `os.environ`, `setenv`, `delenv`. A write to an
attribute of an imported name is the same patch under another spelling and is counted
the same way, keyed by its form and target: `mod.x = v`, `mod.x += v`, `mod.x: T = v`
(form `assignment`), `del mod.x` (`del`), `setattr(mod, "x", v)` and `delattr(mod, "x")`,
and `mod.__dict__["x"] = v` or `vars(mod)["x"] = v` (`__dict__ write`). Only a name the
file's imports resolve is a patch: a parameter, `self.x` or a class defined in the same
file is not, and a module may write to itself. A target is
classified through the file's imports; a string target names a dotted path. A
first-party string target stays `restricted-python`, since it hides the dependency
from the checker, and is not counted twice; an unreadable string target is
`restricted-python` too. `setitem` and `delitem` on other dicts are not patches
here. It applies to fixtures and production code that patch, not only to tests.

## A name the module does not bind

A patch of a name that a first-party module never binds changes nothing the module
reads: `monkeypatch.setattr(mod, "x", v, raising=False)` and `mod.x = v` quietly create
an attribute, and the test passes whatever the code does. That is `missing-patch-target`,
a hard error that cannot be recorded as debt. It applies to every form above and to
`mock.patch.object` / `patch.multiple` on a first-party module, and `raising=False` does
not excuse it. A name counts as bound when the module defines or imports it at its top
level (also under `if`, `try`, `for`, `with` or a walrus, or declared `global` in a
function), is a builtin, is a submodule of the package, or is `__path__` of a package;
a module with a module-level `__getattr__`, a star import or a `globals()` call serves
names at run time and is not checked. When a patch targets a name that the module only
imported from elsewhere rather than defining itself (`from a import f` patched as `b.f`),
the diagnostic flags `(imported binding)` and instructs the caller to inject the dependency
or patch the defining module. An object reached through a module (a class, an
instance) is not checked, only counted.

## The same seam in other shapes

Two codes are not a patch call, but they are the same defect, and review kept finding them
after the patch calls were gone. Both are recorded like the patch codes: the key is
`file#form target`, the debt is the owning directory's, and new ones fail.

**`patch-raising-false`.** `monkeypatch.setattr(..., raising=False)` makes a patch of a
name that no longer exists a silent no-op, so the test keeps passing while it stops testing
what it claims. It is recorded next to the patch's own code (one site, two records, the same key) and is `form target`, for
example `test_x.py#monkeypatch.setattr lib.mod.helper`. On a first-party module the same
call with a name the module does not bind is `missing-patch-target`, which cannot be
recorded; this code covers every other target, the sites that already exist in the
repository, and the day the name is still bound. `delattr`, `delenv` and `delitem` with
`raising=False` are not this code: deleting a name that may be absent is what they are for.
Their actual patches remain counted by the patch family; only the extra missing-target code
is exempt.

**`test-subclass-override`.** A class in a test that overrides a method of a first-party class
replaces behavior by name. It is recorded when all of these hold: the file is a test (a
`tests/` directory, `test_*.py`, `*_test.py` or `conftest.py`, so a support module under
`tests/` counts); a base of the class resolves through the file's imports, following
re-exports, to a class defined in a first-party file that is not a test and not the same file;
that class is neither a `Protocol` nor abstract (it has `ABC` or `Protocol` among its bases, an
`ABCMeta` metaclass or an `@abstractmethod`); and the subclass defines a method, other than a
dunder, that the base class or a first-party ancestor defines. One record per overridden
method, keyed by the defining module: `test_x.py#subclass lib.engine.Engine.run`. A fake that
implements a port or an abstract base, a subclass that adds methods only, and a base that
lives in tests are not recorded.

Both targets are zero, not a long-term debt: the recorded sites are there so that none is
added. What to do instead is in
[test seams in another shape](../../../docs/conventions/python-conventions.md#test-seams-in-another-shape).

## Recording

Every patch code is ordinary debt, owned by the directory of the patching file and
keyed by form and target without a line number:

```toml
[debt.stdlib-patch]
"test_x.py#monkeypatch.setattr time.sleep" = 2
```

New patches fail in a strict-to-be directory like any other violation; repaid ones
make the entry `debt-paid`. A new `unresolved-patch` is no way around the rule:
only what is already in the baseline passes, and CI fails when the recorded total
grows. See [[meizel/docs/checker/debt.meiva.okf.md|recorded debt]]. When a symbol is
renamed, `debt carry --rename-target OLD=NEW` moves the entries to the new name.

Parent: [[meizel/docs/checker/checker.meiva.okf.md|the checker]].
