from __future__ import annotations

import json

import pytest

from meizel.graph.model import OS_KINDS, Acquisition
from meizel.language.typescript_compiler.decode import decode_batch

PATH = "ui/web/src/x.ts"


def result(kind: str) -> str:
    source = {
        "path": PATH,
        "facts": [],
        "external": [],
        "patches": [],
        "purity": [],
        "problems": [],
        "exports": None,
        "defined": [],
        "ambient": [[3, kind, "process.env"]],
        "casts": [],
        "any": [],
        "subsets": [],
        "symbols": [],
        "restricted": [],
    }
    return json.dumps({"version": 1, "sources": [source]})


@pytest.mark.parametrize("kind", [*OS_KINDS, "settings"])
def test_a_kind_the_policy_accepts_is_decoded(kind: str) -> None:
    scans = decode_batch(result(kind), frozenset({PATH}), (*OS_KINDS, "settings"))
    assert scans[PATH].ambient == (Acquisition(3, kind, "process.env"),)


@pytest.mark.parametrize("kind", ["envrion", "settings", ""])
def test_a_kind_the_compiler_misspells_or_the_policy_does_not_declare_is_refused(
    kind: str,
) -> None:
    with pytest.raises(ValueError, match="unknown ambient kind") as refused:
        decode_batch(result(kind), frozenset({PATH}), OS_KINDS)
    assert repr(kind) in str(refused.value)
