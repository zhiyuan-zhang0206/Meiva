# meiva:note
# The single answer to "are these bytes a source file's text?", shared by the lint toolbox and the
# lints' own rules. It is a decision, not an effect: it reads nothing, so a caller that already
# holds the bytes (a walk that read them, a diff that was given them) can ask without paying for a
# second read. `lint_common.read_utf8_text` is the reader, and it decides with this.
#
# LEAF MODULE, and it stays one. Nothing here imports another `meizel` module, so every module may
# import it and no edge can become two-way. The shared untyped readers below live here for exactly
# that reason: their usual home is `lint_common.py`, the toolbox for cross-package helpers, but
# `lint_common` already depends on `graph`, `roles.py` and `checkout.py` -- the three callers that
# need these readers -- so a reader placed there would be imported back. Dependency direction is
# the hard constraint and the module name is the soft one, so the readers take the leaf's name.
# Debt: give them a name that says untyped reader (a `meizel/untyped.py`) once the top-level entry
# budget has room, or once `source_text.py` is renamed for what it now holds. The trigger is the
# next caller that needs an untyped read from `graph`/`roles.py`/`checkout.py`.
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TypeGuard

__all__ = [
    "decode_source",
    "is_mapping",
    "is_object_list",
    "object_mapping",
    "object_of",
    "object_pairs",
]

# meiva:note
# A NUL byte in the first 8 KiB marks a binary file — a NUL later in a genuine text file (one
# inside a string literal) does not. Undecodable bytes are not source either.
_BINARY_WINDOW = 8192


# meiva:note
# The text these bytes are, or None when they are not source text.
def decode_source(data: bytes) -> str | None:
    if b"\x00" in data[:_BINARY_WINDOW]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


# meiva:note
# A call whose answer the standard library types as `Any`, read as `object`.
#
# `json.loads` and `tomllib.loads` hand back `Any`. A function passed as an argument keeps its
# signature outside the callee, so this accepts it as `Callable[P, object]` and returns `object`:
# the caller narrows with `isinstance` instead of holding an unchecked value, and the `Any` is
# never bound to a name. Annotating the local instead does not work -- `raw: object = json.loads(t)`
# is still an `Any` read, and basedpyright says so.
#
# DELIBERATE TWIN, not an accident and not an oversight. The same eight lines live in
# `base/host/boundary/untyped.py` and `cli/fleet_update/untyped.py`. `meizel` is the checker: it
# runs from pre-commit and from `check.sh` inside a bare checkout where no `base` package exists,
# so this package takes no cross-package `[uses]` edge (`meizel.toml`) and must not import one of
# its twins. `meizel/contracts/cli.py::read_args` is the same twin for argparse, for the same
# reason. Changing the rules of one belongs in all three, and merging them breaks the bare
# checkout.
def object_of[**P](function: Callable[P, object], /, *args: P.args, **kwargs: P.kwargs) -> object:
    return function(*args, **kwargs)


# meiva:note
# A mapping the standard library types with `Any` values (`tomllib.loads`) read with `object`
# values: the reader narrows each entry it uses instead of holding an unchecked one. The parameter
# annotation does the work; assigning the mapping to a local annotated `Mapping[str, object]` would
# narrow back to the declared `Any`-valued type.
def object_mapping(mapping: Mapping[str, object], /) -> Mapping[str, object]:
    return mapping


# meiva:note
# `isinstance(value, Mapping)` narrows to `Mapping[Unknown, Unknown]`, and the unknown-type rules
# are errors here, so a narrowed `object` cannot be iterated without a guard. This one only names
# the object-valued mapping; the key check reads it through that narrowing, which is why the body
# yields `object` keys and not `Unknown` ones.
def _is_mapping_of_objects(value: object, /) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, Mapping)


# meiva:note
# A mapping whose keys are all strings: the shape every reader of decoded JSON or TOML wants, and
# the one `isinstance(value, Mapping)` alone cannot promise.
def is_mapping(value: object, /) -> TypeGuard[Mapping[str, object]]:
    return _is_mapping_of_objects(value) and all(isinstance(key, str) for key in value)


# meiva:note
# The list twin of `is_mapping`: a narrowed `object` that is a list is `list[Unknown]` without it,
# and holding or passing `Unknown` is an error.
def is_object_list(value: object, /) -> TypeGuard[list[object]]:
    return isinstance(value, list)


# meiva:note
# The sequence twin of `is_object_list`: `isinstance(value, Sequence)` narrows to
# `Sequence[Unknown]`, whose items no parameter accepts. Private because `object_pairs` is the only
# caller and this module's public surface is at its budget.
def _is_object_sequence(value: object, /) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence)


# meiva:note
# The `(first, second)` of every row of a sequence of sequences. `sqlite3.Cursor.fetchall()` is
# `list[Any]`, and the rows are neither sequences nor pairs as far as the type checker knows; a row
# that is not a pair fails here, where unpacking it failed before.
def object_pairs(rows: object, /) -> list[tuple[object, object]]:
    if not _is_object_sequence(rows):
        raise TypeError(f"rows are {type(rows).__name__}, not a sequence")
    pairs: list[tuple[object, object]] = []
    for row in rows:
        if not _is_object_sequence(row):
            raise TypeError(f"a row is {type(row).__name__}, not a sequence")
        pairs.append((row[0], row[1]))
    return pairs
