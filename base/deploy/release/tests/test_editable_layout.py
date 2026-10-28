# meiva:note
# Where the records of an editable install and its interpreter live, asserted as paths and values:
# the pointer lines to resolve, the comparison key, the pointer beside a metadata record, the
# interpreters and console script of a virtualenv and the checkout an interpreter path belongs to.
# `test_editable_install.py` reads real virtualenvs with them.

from __future__ import annotations

from pathlib import Path

import pytest

from base.deploy.release.editable_layout import (
    console_script_beside,
    layout_source_root,
    pointer_lines,
    pointers_beside,
    pointers_without_metadata,
    venv_python_candidates,
)


@pytest.mark.parametrize(
    ("text", "lines"),
    [
        ("/a\n/b\n", ("/a", "/b")),
        ("/a\n\n/a\n", ("/a", "/a")),
        ("\n\n", ()),
        ("", ()),
        ("  /padded  \n", ("  /padded  ",)),
        ("/a\r\n/b\r\n", ("/a", "/b")),
    ],
)
def test_a_pointer_names_one_source_per_non_blank_line(text: str, lines: tuple[str, ...]) -> None:
    assert pointer_lines(text) == lines


def test_a_metadata_record_expects_its_pointer_in_the_same_site_packages() -> None:
    site = Path("/r/.venv/lib/python3.12/site-packages")
    lib64 = Path("/r/.venv/lib64/python3.11/site-packages")

    assert pointers_beside(
        [site / "meiva-0.1.dist-info" / "direct_url.json", lib64 / "meiva-0.1.dist-info" / "x"],
        "_editable_impl_meiva.pth",
    ) == frozenset({site / "_editable_impl_meiva.pth", lib64 / "_editable_impl_meiva.pth"})


def test_no_metadata_expects_no_pointer() -> None:
    assert pointers_beside([], "_editable_impl_meiva.pth") == frozenset()


def test_the_virtualenv_interpreters_are_python3_then_python() -> None:
    assert venv_python_candidates(Path("/r")) == (
        Path("/r/.venv/bin/python3"),
        Path("/r/.venv/bin/python"),
    )


def test_the_console_script_sits_beside_the_interpreter() -> None:
    assert console_script_beside(Path("/r/.venv/bin/python3")) == (
        Path("/r/.venv/bin/meiva"),
        "/r/.venv/bin/meiva editable console script missing",
    )


@pytest.mark.parametrize(
    ("interpreter", "root"),
    [
        ("/work/meiva/.venv/bin/interp", "/work/meiva"),
        ("/work/meiva/.venv/bin/interp3.12", "/work/meiva"),
        ("/.venv/bin/interp", "/"),
    ],
)
def test_an_interpreter_under_a_checkout_venv_names_its_checkout(
    interpreter: str, root: str
) -> None:
    assert layout_source_root(Path(interpreter)) == Path(root)


@pytest.mark.parametrize(
    "interpreter",
    [
        "/usr/bin/interp3",
        "/work/meiva/.venv/lib/interp",
        "/work/meiva/venv/bin/interp",
        "/work/.venv/interp",
        "interp",
        "/interp",
    ],
)
def test_an_interpreter_outside_the_layout_belongs_to_no_checkout(interpreter: str) -> None:
    assert layout_source_root(Path(interpreter)) is None


def test_a_pointer_without_a_dist_info_in_its_directory_is_without_metadata() -> None:
    site = Path("/r/.venv/lib/python3.12/site-packages")
    other = Path("/r/.venv/lib64/python3.12/site-packages")
    record = site / "meiva-0.1.dist-info" / "direct_url.json"

    assert pointers_without_metadata(
        [other / "_editable_impl_meiva.pth", site / "_editable_impl_meiva.pth"], [record]
    ) == (other / "_editable_impl_meiva.pth",)


def test_pointers_without_metadata_are_ordered_by_text() -> None:
    a = Path("/s/a/_editable_impl_meiva.pth")
    b = Path("/s/a.x/_editable_impl_meiva.pth")

    assert pointers_without_metadata([a, b], []) == (b, a)


def test_with_no_metadata_every_pointer_is_without_it_sorted_and_distinct() -> None:
    a = Path("/s/b/_editable_impl_meiva.pth")
    b = Path("/s/a/_editable_impl_meiva.pth")

    assert pointers_without_metadata([a, b, a], []) == (b, a)
    assert pointers_without_metadata([], [a]) == ()
