# meiva:note
# The editable-install policy judges values; no file is touched.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.deploy.release.editable_policy import (
    EditableReadings,
    MetadataRecord,
    PointerRecord,
    allowed_forms,
    direct_url_is_allowed,
    editable_violations,
    exact_path_key,
    pointer_is_allowed,
    sorted_paths,
)

_ROOT = "/srv/meiva/source"
_ROOTS = frozenset({_ROOT, "/srv/dev/Meiva"})
_URLS = frozenset({"file:///srv/meiva/source"})
_SITE = Path("/srv/meiva/source/.venv/lib/python3.12/site-packages")
_PTH = _SITE / "_editable_impl_meiva.pth"
_DIRECT_URL = _SITE / "meiva-1.0.0.dist-info" / "direct_url.json"


def _pointer(text: str | None, targets: tuple[str, ...] | None) -> PointerRecord:
    return PointerRecord(path=_PTH, text=text, targets=targets)


def _record(url: str = "file:///srv/meiva/source", **dir_info: object) -> str:
    return json.dumps({"url": url, "dir_info": {"editable": True, **dir_info}})


@pytest.mark.parametrize(
    ("record", "allowed"),
    [
        (_pointer(_ROOT, (_ROOT,)), True),
        (_pointer(f"{_ROOT}\n", (_ROOT,)), True),
        (_pointer(f"{_ROOT}\r\n{_ROOT}\r\n", (_ROOT, _ROOT)), True),
        (_pointer("/srv/dev/Meiva", ("/srv/dev/Meiva",)), True),
        (_pointer("/srv/dev/Meiva/.worktrees/x", ("/srv/dev/Meiva/.worktrees/x",)), False),
        (_pointer(f"{_ROOT}\n/elsewhere", (_ROOT, "/elsewhere")), False),
        (_pointer("", ()), False),
        (_pointer(f"{_ROOT}\n\n{_ROOT}", (_ROOT, _ROOT)), False),
        (_pointer(_ROOT, None), False),
        (_pointer(None, None), False),
    ],
)
def test_a_pointer_is_legal_only_when_every_line_names_an_allowlisted_root(
    record: PointerRecord, allowed: bool
) -> None:
    assert pointer_is_allowed(record, _ROOTS) is allowed


@pytest.mark.parametrize(
    ("raw", "allowed"),
    [
        (_record(), True),
        (_record("file:///srv/meiva/source/"), True),
        (_record("file:///srv/dev/Meiva"), False),
        (json.dumps({"url": "file:///srv/meiva/source", "dir_info": {"editable": False}}), False),
        (json.dumps({"url": "file:///srv/meiva/source", "dir_info": {}}), False),
        (json.dumps({"url": "file:///srv/meiva/source"}), False),
        (json.dumps({"dir_info": {"editable": True}}), False),
        (json.dumps(["file:///srv/meiva/source"]), False),
        ("{not json", False),
        ("  \n", False),
    ],
)
def test_a_direct_url_record_must_name_an_allowed_root_as_an_editable_install(
    raw: str, allowed: bool
) -> None:
    assert direct_url_is_allowed(raw, _URLS) is allowed


def _readings(
    pointers: tuple[PointerRecord, ...] = (),
    metadata: tuple[MetadataRecord, ...] = (),
    missing: tuple[Path, ...] = (),
    without_metadata: tuple[Path, ...] = (),
) -> EditableReadings:
    return EditableReadings(
        pointers=pointers,
        metadata=metadata,
        pointers_missing=missing,
        pointers_without_metadata=without_metadata,
    )


def _violations(readings: EditableReadings) -> tuple[str, ...]:
    return editable_violations(readings, allowed_roots=_ROOTS, allowed_urls=_URLS)


def test_nothing_observed_is_clean_and_so_is_a_legal_install() -> None:
    assert _violations(_readings()) == ()
    legal = _readings(
        pointers=(_pointer(_ROOT, (_ROOT,)),), metadata=(MetadataRecord(_DIRECT_URL, _record()),)
    )
    assert _violations(legal) == ()


def test_each_kind_of_violation_is_worded_with_the_path_it_concerns() -> None:
    readings = _readings(
        pointers=(_pointer("/elsewhere", ("/elsewhere",)),),
        metadata=(MetadataRecord(_DIRECT_URL, _record("file:///elsewhere")),),
        missing=(Path("/a/_editable_impl_meiva.pth"),),
        without_metadata=(Path("/b/_editable_impl_meiva.pth"),),
    )

    assert _violations(readings) == (
        "/a/_editable_impl_meiva.pth editable install metadata present but pointer missing"
        " — half-uninstalled",
        "/b/_editable_impl_meiva.pth editable pointer present but metadata missing"
        " — half-uninstalled",
        f"{_PTH} names '/elsewhere'; non-allowlisted source or is inconsistent",
        f"{_DIRECT_URL} records {_record('file:///elsewhere')!r}",
    )


def test_an_unreadable_or_empty_record_is_a_violation_not_a_crash() -> None:
    readings = _readings(
        pointers=(_pointer(None, None),), metadata=(MetadataRecord(_DIRECT_URL, None),)
    )
    assert _violations(readings) == (f"{_PTH} unreadable", f"{_DIRECT_URL} unreadable")

    empty = _readings(pointers=(_pointer("", ()),), metadata=(MetadataRecord(_DIRECT_URL, ""),))
    assert _violations(empty) == (
        f"{_PTH} names '(empty)'; non-allowlisted source or is inconsistent",
        f"{_DIRECT_URL} records '(empty)'",
    )


def test_a_listing_of_paths_is_distinct_and_ordered_by_text() -> None:
    a, b, c = Path("/s/a.pth"), Path("/s/b.pth"), Path("/s/B.pth")

    assert sorted_paths([b, a, c, a]) == (c, a, b)
    assert sorted_paths([]) == ()
    assert sorted_paths([Path("/s/a/x"), Path("/s/a.x")]) == (Path("/s/a.x"), Path("/s/a/x"))


def test_the_comparison_key_is_the_exact_spelling_of_the_resolved_path() -> None:
    assert exact_path_key(Path("/Work/Meiva")) == "/Work/Meiva"


def test_the_checkout_and_the_roots_given_are_allowed_in_both_spellings() -> None:
    roots, urls = allowed_forms(Path("/srv/meiva/source"), [Path("/home/u/Meiva"), Path("/a b")])

    assert roots == frozenset({"/srv/meiva/source", "/home/u/Meiva", "/a b"})
    assert urls == frozenset({"file:///srv/meiva/source", "file:///home/u/Meiva", "file:///a%20b"})


def test_the_checkout_alone_is_allowed_when_no_other_root_is_given() -> None:
    assert allowed_forms(Path("/srv/meiva/source"), []) == (
        frozenset({"/srv/meiva/source"}),
        frozenset({"file:///srv/meiva/source"}),
    )
