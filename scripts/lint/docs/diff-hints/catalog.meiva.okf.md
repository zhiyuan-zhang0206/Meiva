---
type: doc
title: Diff hints catalog
description: Every diff hint id with what it fires on and the first recipe it prints, the shapes the hints cannot see, and the steps for adding a hint.
tags:
- scripts
- lint
- hooks
---

# Diff hints catalog

Each hint prints the site, then once per id: `why` (the principle in a sentence), `instead`
(the recipe for this id in order of preference) and `see` (the rule). The text is in
`scripts/lint/diagnostics/diff_hints/catalog.py:spec_of`; the rules are in
[Python conventions](../../../../docs/conventions/python-conventions.md).

| Id | Fires on (added lines) | Instead, first choice |
|---|---|---|
| `ambient-read` | production file, not a root: the wall clock `time.time`, an interval clock (`time.monotonic`, `time.perf_counter`) only in a function that also sleeps, `datetime.now/utcnow/today`, `os.environ`, `os.getenv`, `os.getcwd`, `Path.home/cwd`, `platform.*`, `sys.platform`, `socket.gethostname`, `random.*`, a read of `settings`; not a clock handed to a call as `now=`/`*_at`/`*_ts` | take the value; lift a loop's decision into a pure function of `now` and keep the sleep in the shell |
| `callable-default` | a parameter annotated `Callable`, `Coroutine` or `Awaitable`, or named `*_fn`, `*_hook`, `*_cb`, `*_reader`, `*_factory`, `*_fetch`, with a default | required parameter; a value if it only fetches one; a plan if it is an effect |
| `callable-bag` | a dataclass, `NamedTuple`, `TypedDict`, `Protocol` or `BaseModel` with two or more `Callable` fields | values in, a plan out; one narrow port in `base/` for a real effect |
| `none-fallback` | production file: a `None` default and `x or Call()`, `if x is None: x = Call()` or `Call() if x is None else x` | required parameter; split a borrowed and an owned mode into two types |
| `wildcard-case` | production file: a `case _:` that neither raises nor calls `assert_never`; a `str` parameter named action, status, role, kind, mode, op or verb compared with literals in a function that never raises | every member plus `assert_never`; a `Literal` or enum; `ValueError` at the boundary |
| `protocol-outside-base` | a `Protocol` class outside `base/` | the port in `base/` with its fake in that layer's `tests/`; the real type for a data shape |
| `any-collaborator` | production file: a parameter typed `Any` or `object` whose name is a collaborator (client, pool, conn, config, context, graph, service, ...) | the real type or a narrow port; a parser's `value: object` is not matched |
| `deleted-test` | a removed `def test_*` whose name the same diff does not add back | list each in the PR description with its reason; a pure function's test moves with it |
| `service-locator` | production file, not a root: `request.app.state.*`, `app.state.*`, `runtime_view(...)`, `settings_view(...)` | a frozen `XxxConfig` built by the root with exactly the fields read |
| `mutable-in-frozen` | production file: a frozen dataclass field typed `list`, `dict`, `set`, `object`, `Any` (or the `typing` and `collections` spellings) | a tuple, frozenset or `Mapping`; return what to append and let the shell append |
| `test-seam` | test file: `vars(x)[..] =`, `x.__dict__[..] =`, `object.__setattr__`, `raising=False`, `sys.modules[..] =`, a write to `os.environ`, `.side_effect =`, an attribute assigned on an imported name or on a fixture object of a test function, a method replaced by a lambda or function, `httpx.MockTransport`, `inspect.getsource`, a `time.sleep` in the body of a `test_*` function, outside a loop | a real resource makes the fault, or a narrow port in `base/` with its fake; the spelling is not the repair |
| `test-subclass` | test file: a class with a method whose base is a production class (resolved through imports to a first-party non-test module) or a `psycopg`, `psycopg_pool` or `redis` class; exception bases are skipped | give the class the value it reads as a constructor field; a real resource for the fault |
| `lambda-dependency` | a call passing `lambda` for a `*_reader`, `*_fn` or `*_hook` keyword, or for a `*_factory` keyword when the lambda only returns a name, attribute or constant | pass the value; change the lower API and name its owner |
| `spy-assert` | test file: `assert_called*`, `assert_awaited*`, `assert_not_called`, `assert_has_calls`, `assert_any_call`, `call_args`, `call_count`, `mock_calls` and the await spellings | assert the plan or the real effect's result |

The same rules in the order `instead` gives them are "data in, decisions out"
([the forms](../../../../docs/conventions/python-conventions.md#data-in-decisions-out-the-forms-in-order)).

## Known gaps

The hints see added lines, so they do not see what a change leaves behind in untouched code
(9 of the 73 reviewed findings were leftovers), and they cannot see behavior (22 were
semantic, undeclared behavior changes or weakened assertions). Three shapes are mechanical
but not caught: a field compared with a string literal (`fact.role == "shell"`), because
syntax cannot tell a closed vocabulary from a wire value; a required callable typed as a
`Callable` type alias (`DecideCall = Callable[..., Awaitable[...]]`); and an if-chain over
enum members without a final `else` (only `match` is looked at).

The diff hint now asks about `raising=` set by a variable, `**{...}` or a fourth positional
argument; these shapes remain tolerated misses of the deterministic meizel code.
Other gaps closed after review: a `case _:` body fails only when it ends in a `raise` or
`assert_never`; a selector is guarded only by a `raise` in its own body, not a nested def;
`none-fallback` also sees `x or default_name` (an imported or module-level implementation,
not a constant), `x if x else Foo()`, an annotated assignment and `self.x = Foo()`;
`mutable-in-frozen` asks when the `@dataclass(frozen=True)` line is the added one;
`callable-default` judges any added line of a parameter's name, annotation and default and sees
`Callable as Cb`; and `deleted-test` treats a name as moved only when a file of the same base
name adds it. Still open: a `frozen=FLAG` that is not a literal, a `frozen` set through
`dataclasses.dataclass(...)` applied as a function, and the pre-push stage judging `HEAD` rather
than the pushed ref (the ref arrives in an environment variable that the lint on `os.environ`
keeps tooling from reading).

## Adding or changing a hint

1. Write the detector in a module of `scripts/lint/diagnostics/diff_hints/` as a pure function
   of a `Unit` (`model.py`); keep the module `strict` in its `meizel.toml`.
2. Add the id to `HintId` and its text to `scripts/lint/diagnostics/diff_hints/catalog.py:spec_of`; the match is exhaustive, so the
   catalog cannot lag. Point `see` at a real heading.
3. Add a positive and a negative case to `tests/test_detectors.py` and a row to the table above.
4. Run `hints.py report` over the five pull requests above and read a sample of the hits. A
   hint whose sample is mostly misfires is narrowed before it ships; the numbers go in this page.

Parent: [[scripts/lint/docs/diff-hints/diff-hints.meiva.okf.md|diff-hints]].
