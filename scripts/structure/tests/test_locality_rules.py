from __future__ import annotations

import ast
import pathlib

import pytest

from meizel import allowances
from meizel.owners import decisions
from scripts.structure import locality


def _parse(source: str) -> ast.Module:
    return ast.parse(source)


# meiva:note
# The decisions this module measures against are the caller's value, as in the gate: the policy read
# here names one owner and grants nothing, so no test depends on the real exemption list.
_POLICY = '[single-owner]\npostgres-dial-owners = ["base/db/connections.py"]\n'
_TABLE = decisions.decision_table(allowances.parse(_POLICY)[0])


def test_test_directory_is_exempt_but_a_test_prefixed_governed_file_is_scanned() -> None:
    source = "import psycopg\npsycopg.connect('dsn')\n"

    assert locality.measure(_parse(source), "gateway/tests/x.py", ("a",), _TABLE) == {
        "owner_bypasses": {}
    }
    measured = locality.measure(_parse(source), "gateway/test_db.py", ("a",), _TABLE)
    assert measured["owner_bypasses"] == {"gateway/test_db.py::postgres-dial": [2]}


def test_measure_scans_a_non_test_file_for_the_decision_owner_rule() -> None:
    source = "import psycopg\npsycopg.connect('dsn')\n"

    measured = locality.measure(_parse(source), "gateway/db.py", ("a",), _TABLE)

    assert measured == {"owner_bypasses": {"gateway/db.py::postgres-dial": [2]}}


def test_site_errors_reports_every_occurrence_in_line_order(tmp_path: pathlib.Path) -> None:
    errors = locality.site_errors(
        {"owner_bypasses": {"gateway/db.py::postgres-dial": [9, 5]}},
        {},
        scanned={"gateway/db.py"},
        repo_root=tmp_path,
        table=_TABLE,
    )
    assert len(errors) == 2
    assert errors[0].startswith("gateway/db.py:5: bypasses the single owner of `postgres-dial`")
    assert errors[1].startswith("gateway/db.py:9: bypasses the single owner of `postgres-dial`")
    assert all("frozen" not in error for error in errors)


def test_site_errors_gives_no_rename_allowance(tmp_path: pathlib.Path) -> None:
    errors = locality.site_errors(
        {"owner_bypasses": {"gateway/db_new.py::postgres-dial": [5]}},
        {},
        scanned={"gateway/db_new.py"},
        repo_root=tmp_path,
        table=_TABLE,
        renames={"gateway/db_old.py": "gateway/db_new.py"},
    )
    assert len(errors) == 1
    assert "bypasses the single owner of `postgres-dial`" in errors[0]
    assert "migrate" not in errors[0]


def test_validate_entries_rejects_a_non_dict_section() -> None:
    with pytest.raises(ValueError, match="'owner_bypasses' must be an object"):
        locality.validate_entries("owner_bypasses", [], ("a",), _TABLE)


@pytest.mark.parametrize(
    "entry",
    [
        "a/mod.py",
        "a/mod.py::",
        "a/mod.py::not-a-decision",
        "/a/mod.py::postgres-dial",
        "a/../mod.py::postgres-dial",
        "c/mod.py::postgres-dial",
        "a/mod.txt::postgres-dial",
    ],
)
def test_validate_owner_bypasses_rejects_malformed_entries(entry: str) -> None:
    with pytest.raises(ValueError, match="invalid owner_bypasses entry"):
        locality.validate_entries("owner_bypasses", {entry: 1}, ("a",), _TABLE)


@pytest.mark.parametrize("count", [0, -1, True, "1", 1.0, None])
def test_validate_owner_bypasses_rejects_bad_counts(count: object) -> None:
    with pytest.raises(ValueError, match="invalid owner_bypasses entry"):
        locality.validate_entries(
            "owner_bypasses", {"a/mod.py::postgres-dial": count}, ("a",), _TABLE
        )


def test_validate_owner_bypasses_accepts_a_known_decision_name() -> None:
    locality.validate_entries("owner_bypasses", {"a/mod.py::postgres-dial": 1}, ("a",), _TABLE)
