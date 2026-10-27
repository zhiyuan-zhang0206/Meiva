# meiva:note
# What each hint says when it fires: the principle (`why`), the recipe for this id in
# order of preference (`instead`) and the document that holds the rule (`see`, a
# repository path with a heading anchor that exists).
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from scripts.lint.diagnostics.diff_hints.model import MIN_IMPORTERS, MIN_ONWARD, HintId

_CONVENTIONS = "docs/conventions/python-conventions.md"
_FORMS = f"{_CONVENTIONS}#data-in-decisions-out-the-forms-in-order"
_LANDS = f"{_CONVENTIONS}#where-each-kind-of-dependency-lands"
_ROOTS = f"{_CONVENTIONS}#composition-roots-and-required-dependencies"
_VALUES = f"{_CONVENTIONS}#types-of-collaborators-and-values"
_SEAMS = f"{_CONVENTIONS}#test-seams-in-another-shape"
_DELETED = f"{_CONVENTIONS}#deleting-or-rewriting-a-test"
_DOMAIN = f"{_CONVENTIONS}#finite-domain-vocabulary"


@dataclass(frozen=True)
class HintSpec:
    why: str
    instead: str
    see: str


def _spec_callable_default() -> HintSpec:
    return HintSpec(
        why=(
            "a callable parameter with a default hides a dependency that no caller sees, and a "
            "bag of callbacks is a spy seam: the test then asserts the calls instead of the "
            "decision."
        ),
        instead=(
            "1) if the callable only fetches a value, take the value; 2) if it is an effect "
            "inside logic, lift the decision into a pure function that returns the plan and let "
            "the shell call the real implementation; 3) test the shell with a real resource; "
            "4) only an effect the shell cannot lift takes a required, narrow Protocol from "
            "base/, never a default that reaches the real implementation."
        ),
        see=_FORMS,
    )


def _spec_callable_bag() -> HintSpec:
    return HintSpec(
        why=(
            "a class that bundles several callables is a dependency bag: every field is a "
            "hidden effect and the tests replace the fields one by one, which is a patch in "
            "another shape."
        ),
        instead=(
            "1) replace the fields that fetch a value with the values; 2) lift the decisions "
            "that use the effects into pure functions that return a plan, and let one shell "
            "execute it; 3) test the shell against a real resource; 4) a genuine effect becomes "
            "one narrow Protocol defined in base/. A table of pure functions (a step table, a "
            "validator table) is not a bag."
        ),
        see=_VALUES,
    )


def _spec_none_fallback() -> HintSpec:
    return HintSpec(
        why=(
            "`x or Real()` and `if x is None: x = Real()` give a dependency a default that "
            "reaches the real implementation: a caller can forget it unnoticed and the function "
            "becomes a second composition root."
        ),
        instead=(
            "1) make the parameter required and let the root pass the value or object; 2) if "
            "the function serves both a borrowed and an owned resource, split them into two "
            "types instead of discriminating on None; 3) and 4) as for callable-default. A "
            "default is for tuning constants only."
        ),
        see=_ROOTS,
    )


def _spec_wildcard_case() -> HintSpec:
    return HintSpec(
        why=(
            "`case _` and an implicit else turn an unknown or newly added value into silent "
            "behavior and bypass the exhaustiveness check that pyright strict already does; a "
            "`str` selector hides the domain from the reader."
        ),
        instead=(
            "1) list every member explicitly and end with `case _: assert_never(x)`; 2) turn the "
            "`str` selector into the enum that owns the domain (a `Literal` where a schema or protocol "
            "requires one); 3) convert "
            "and reject unknown values at the boundary with `ValueError`; 4) an if-chain ends "
            "in an `else` that raises."
        ),
        see=_DOMAIN,
    )


def _spec_protocol_outside_base() -> HintSpec:
    return HintSpec(
        why=(
            "an effect port defined above base/ is a second vocabulary for the same effect, "
            "and its fake ends up beside a single consumer; ports have one owner so that every "
            "layer's tests share one fake."
        ),
        instead=(
            "1) if it is an effect, define the port in base/ with its fake in that layer's "
            "tests/ and export both; 2) if it only describes the shape of data, use the real "
            "type, a heavy import is not a reason for a Protocol; 3) check the plan and shell "
            "split first, a decision lifted into a pure function needs no port; 4) if base "
            "cannot own it, say why."
        ),
        see=_VALUES,
    )


def _spec_any_collaborator() -> HintSpec:
    return HintSpec(
        why=(
            "an `Any` or `object` collaborator switches the type checker off at the boundary: "
            "tests pass anything and the contract of the parameter exists only in the "
            "author's head."
        ),
        instead=(
            "1) write the real type (the owner's class, a frozen config, or a narrow Protocol "
            "from base/); 2) a heavy import is not a reason for `Any`, import the real type; "
            "3) for a value that is data, use its value type; 4) for a payload that really is "
            "opaque, a TypeVar or a union of the real members. A parser's `value: object` is "
            "raw input and stays `object`."
        ),
        see=_VALUES,
    )


def _spec_deleted_test() -> HintSpec:
    return HintSpec(
        why=(
            "a deleted test is lost coverage unless something else asserts the same behavior, "
            "and a reviewer cannot see that from the diff, so the author states it."
        ),
        instead=(
            "1) a pure function's test moves with the function and is never deleted; 2) a "
            "rewritten test must map one to one onto the behaviors the old one covered, and no "
            "test double may reproduce production logic; 3) a test of an interface that really "
            "disappeared is deleted with its reason; 4) answer with the trailer below and list "
            "each deleted test and its reason in the PR description."
        ),
        see=_DELETED,
    )


def _spec_service_locator() -> HintSpec:
    return HintSpec(
        why=(
            "reading `app.state`, `runtime_view(...)` or a whole `Settings` from a handler is a "
            "service locator: the inputs of the handler are invisible in its signature and a "
            "test must build the application to vary one field."
        ),
        instead=(
            "1) the component takes a frozen `XxxConfig` built by the root with exactly the "
            "fields it reads (per route or per daemon); 2) values travel down as plain "
            "arguments; 3) test the handler with the config value and a real client; 4) no "
            "second channel such as `runtime_view` beside the config."
        ),
        see=_LANDS,
    )


def _spec_mutable_in_frozen() -> HintSpec:
    return HintSpec(
        why=(
            "a frozen dataclass whose member is a list, dict, set, object or Any is frozen in "
            "name only: the holder is mutated after construction and the value changes under "
            "its readers."
        ),
        instead=(
            "1) use a tuple, a frozenset or a read-only Mapping value; 2) let the plan return "
            "what must be appended and let the shell append it; 3) and 4) a genuinely mutable "
            "resource is an owner with its own lifecycle, not a field of a value."
        ),
        see=_VALUES,
    )


def _spec_test_seam() -> HintSpec:
    return HintSpec(
        why=(
            "a test that rewrites a module, an instance or a mock after construction is a "
            "patch in another shape: it still finds its target by name, bypasses the signature, "
            "and `raising=False` hides the day the name no longer exists."
        ),
        instead=(
            "a fault or a value has two legitimate routes. 1) a real resource makes the fault "
            "(revoke INSERT or UPDATE, a read-only transaction, a closed pool, a held lock, a "
            "trigger that raises, a renamed table, an invalid URL, a full or read-only "
            "directory, a killed child); 2) for a fault no real resource can make (a cancel "
            "that hangs, a half-open connect, a failed fsync), a narrow Protocol in base/ with "
            "its fake in base's tests/. Turning `vars()` into a subclass, or a subclass into an "
            "armed flag, does not count."
        ),
        see=_SEAMS,
    )


def _spec_test_subclass() -> HintSpec:
    return HintSpec(
        why=(
            "a test that subclasses production code to override one method is the same patch: "
            "the subclass replaces behavior by name, and later changes of the base silently "
            "stop being tested."
        ),
        instead=(
            "1) give the production class the value it reads (an identity, parent facts) as a "
            "constructor field, so the test builds the real class with that value; 2) lift the "
            "overridden decision into a pure function; 3) make the fault with a real resource; "
            "4) if that is impossible, a narrow Protocol in base/ with a fake. Answer here only "
            "when the base is a test-support class."
        ),
        see=_SEAMS,
    )


def _spec_lambda_dependency() -> HintSpec:
    return HintSpec(
        why=(
            "passing a `lambda` for a `*_fn`, `*_reader`, `*_factory` or `*_hook` parameter "
            "wraps a value back into a callback: the callee asks for a value in disguise, or "
            "the lower API takes a bag it should not."
        ),
        instead=(
            "1) pass the value itself; 2) if the lower API insists on a callable, change that "
            "API to take the value (name the owner in the PR description when it is another "
            "lane's package); 3) if the callee runs an effect, lift its decision and pass the "
            "plan; 4) otherwise a required narrow port."
        ),
        see=_FORMS,
    )


def _spec_spy_assert() -> HintSpec:
    return HintSpec(
        why=(
            "asserting calls (`assert_called_once_with`, `call_args`, `call_count`) pins how "
            "the code talks to its collaborator, not what it decides: it breaks on a refactor "
            "that keeps behavior and passes when the decision is wrong."
        ),
        instead=(
            "1) assert the plan or the value a pure function returns; 2) if the decision is "
            "not lifted yet, lift it and assert on the result; 3) for a shell, run it against "
            "a real resource and assert the observable outcome (a file, a row, process "
            "output, a socket reply); 4) a recorder of a real wire protocol is an observation, "
            "not a spy."
        ),
        see=_SEAMS,
    )


def _spec_root_middle_layer() -> HintSpec:
    return HintSpec(
        why=(
            f"a file declared a root that {MIN_IMPORTERS} or more modules outside the roots import "
            f"(or one that {MIN_ONWARD} or more import) is a middle layer: the importers depend on "
            "a file that reads the world, so the read is not made once at the process entry and a "
            "test cannot hand the importer a value. A root newly declared in a manifest is always "
            "asked about: say what it is."
        ),
        instead=(
            "1) read the value in the real root (the daemon, the CLI runtime, the wiring "
            "module) and pass it down as a required parameter; 2) if the file is a factory the "
            "process builds once, name its single caller in the Hint-Ack trailer; 3) if the "
            "importers only take a type or a constant from a file that also reads the world, "
            "move those out so the root has no importers; 4) never widen the declaration to "
            "make an importer pass; 5) a module that only wraps the read is an "
            "`[ambient-readers]` entry, not a root."
        ),
        see=_ROOTS,
    )


# meiva:note
# One builder per id. A new `HintId` without a builder fails the catalog test for every
# id, so the text cannot lag the vocabulary.
def _builders() -> dict[HintId, Callable[[], HintSpec]]:
    return {
        HintId.CALLABLE_DEFAULT: _spec_callable_default,
        HintId.CALLABLE_BAG: _spec_callable_bag,
        HintId.NONE_FALLBACK: _spec_none_fallback,
        HintId.WILDCARD_CASE: _spec_wildcard_case,
        HintId.PROTOCOL_OUTSIDE_BASE: _spec_protocol_outside_base,
        HintId.ANY_COLLABORATOR: _spec_any_collaborator,
        HintId.DELETED_TEST: _spec_deleted_test,
        HintId.SERVICE_LOCATOR: _spec_service_locator,
        HintId.MUTABLE_IN_FROZEN: _spec_mutable_in_frozen,
        HintId.TEST_SEAM: _spec_test_seam,
        HintId.TEST_SUBCLASS: _spec_test_subclass,
        HintId.LAMBDA_DEPENDENCY: _spec_lambda_dependency,
        HintId.SPY_ASSERT: _spec_spy_assert,
        HintId.ROOT_MIDDLE_LAYER: _spec_root_middle_layer,
    }


def spec_of(hint: HintId) -> HintSpec:
    return _builders()[hint]()
