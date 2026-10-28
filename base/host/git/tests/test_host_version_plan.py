# meiva:note
# What the host version is made of, asserted as values: the version grammar, how a pyproject text
# and `git log` output become a version and how a derived version is shown. No git runs and no file
# is read; `test_host_version.py` derives versions from real checkouts.

from __future__ import annotations

import tomllib

import pytest

from base.host.git.host_version_plan import (
    PyprojectVersionError,
    Version,
    derived_from_log,
    display_form,
    parse_version,
    unreadable_pyproject_message,
    version_from_pyproject,
)


@pytest.mark.parametrize(
    ("raw", "version"),
    [
        ("1", Version(1)),
        ("1.2", Version(1, 2)),
        ("2026.3.5", Version(2026, 3, 5)),
        ("  0.1.5 \n", Version(0, 1, 5)),
        ("1.2.3-rc1", Version(1, 2, 3, "rc.1")),
        ("1.2.3-beta", Version(1, 2, 3, "b.0")),
        ("1.2.3-alpha.4", Version(1, 2, 3, "a.4")),
        ("1.2.3-Custom", Version(1, 2, 3, "custom")),
    ],
)
def test_a_version_parses_into_three_segments_and_a_normal_prerelease(
    raw: str, version: Version
) -> None:
    assert parse_version(raw) == version


@pytest.mark.parametrize("raw", ["", "x", "1.2.3.4", "v1.2.3", "1..2", "1.2.3-"])
def test_text_that_is_not_a_version_does_not_parse(raw: str) -> None:
    assert parse_version(raw) is None


def test_versions_order_by_segments_and_a_prerelease_sorts_below_its_release() -> None:
    ordered = ["1.2.3-dev", "1.2.3-a1", "1.2.3-b2", "1.2.3-rc1", "1.2.3", "1.2.4", "1.10.0", "2"]
    versions = [parse_version(raw) for raw in ordered]

    assert all(v is not None for v in versions)
    assert sorted(v for v in versions if v is not None) == [v for v in versions if v is not None]
    release = parse_version("1.2.3")
    same = parse_version("1.2.3")
    candidate = parse_version("1.2.3-rc1")
    assert release is not None and same is not None and candidate is not None
    assert candidate < release
    assert not release < same


def test_a_pyproject_with_a_version_yields_it() -> None:
    raw = '[project]\nname = "meiva"\nversion = "0.1.5"\n'

    assert version_from_pyproject(raw, "/r/pyproject.toml") == "0.1.5"


def test_a_pyproject_that_is_not_toml_raises_the_parse_error() -> None:
    with pytest.raises(PyprojectVersionError, match=r"^cannot determine host version: ") as caught:
        version_from_pyproject("[project\nversion = ", "/r/pyproject.toml")

    assert isinstance(caught.value.__cause__, tomllib.TOMLDecodeError)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        '[tool.x]\nversion = "1.0"\n',
        "[project]\nname = 'meiva'\n",
        '[project]\nversion = "not-a-version"\n',
        "[project]\nversion = 5\n",
    ],
)
def test_a_pyproject_without_a_usable_version_names_the_file(raw: str) -> None:
    with pytest.raises(PyprojectVersionError) as caught:
        version_from_pyproject(raw, "/r/pyproject.toml")

    assert str(caught.value) == (
        "cannot determine host version: /r/pyproject.toml [project].version invalid"
    )


def test_a_checkout_without_a_readable_pyproject_is_refused_naming_it() -> None:
    assert unreadable_pyproject_message("/r") == (
        "cannot determine host version: no pyproject.toml in /r"
    )


@pytest.mark.parametrize(
    ("returncode", "stdout", "derived"),
    [
        (0, "2026-03-05|abc1234\n", ("2026.3.5", "abc1234")),
        (0, "2027-11-30|f00\n", ("2027.11.30", "f00")),
        (0, "2026-03-05|\n", ("2026.3.5", "")),
        (0, "  2026-01-02|a1b2c3d  ", ("2026.1.2", "a1b2c3d")),
        (1, "2026-03-05|abc1234\n", None),
        (0, "", None),
        (0, "not-a-date|abc\n", None),
        (0, "2026-03|abc\n", None),
        (0, "2026-03-xx|abc\n", None),
    ],
)
def test_git_log_output_becomes_the_derived_version_or_nothing(
    returncode: int, stdout: str, derived: tuple[str, str] | None
) -> None:
    assert derived_from_log(returncode, stdout) == derived


def test_a_derived_version_is_shown_with_its_sha() -> None:
    assert display_form(("2026.3.5", "abc1234")) == "2026.3.5+gabc1234"


@pytest.mark.parametrize(
    ("raw", "prerelease"),
    [
        ("1.2.3-RC1", "rc.1"),
        ("1.2.3-preview4", "rc.4"),
        ("1.2.3-pre", "rc.0"),
        ("1.2.3-c5", "rc.5"),
        ("1.2.3-rc-1", "rc.1"),
        ("1.2.3-rc.1", "rc.1"),
        ("1.2.3-dev", "dev.0"),
        ("1.2.3-a-b", "a-b"),
    ],
)
def test_prerelease_normal_forms(raw: str, prerelease: str) -> None:
    version = parse_version(raw)

    assert version is not None and version.prerelease == prerelease


def test_an_unknown_prerelease_sorts_after_rc_and_before_the_release() -> None:
    rc = parse_version("1.2.3-rc1")
    custom = parse_version("1.2.3-custom")
    same_custom = parse_version("1.2.3-custom")
    release = parse_version("1.2.3")

    assert rc is not None and custom is not None and same_custom is not None
    assert release is not None
    assert rc < custom < release
    assert not custom < same_custom
    assert Version(1, 2, 3) == Version(1, 2, 3, None)
    assert Version(1, 2, 4) > Version(1, 2, 3)
    assert Version(1, 3, 0) > Version(1, 2, 9)
    assert Version(2, 0, 0) > Version(1, 9, 9)
    assert not Version(1, 2, 3) < Version(1, 2, 3)
