from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from base.host.system.job_values import JobRoots

_ABSOLUTE = {"home": Path("/h/.meiva"), "repo": Path("/h/checkout"), "user_home": Path("/h")}


# meiva:note
# The three fields are all paths, so the type checker cannot see them swapped: construction is by
# keyword only, which makes a swap a visible `home=checkout` at the call site.
def test_the_roots_are_built_by_keyword_only() -> None:
    assert all(field.kw_only for field in dataclasses.fields(JobRoots))


@pytest.mark.parametrize("field", ["home", "repo", "user_home"])
def test_a_relative_root_is_refused_naming_the_field(field: str) -> None:
    with pytest.raises(ValueError, match=f"JobRoots.{field} must be an absolute path"):
        JobRoots(**{**_ABSOLUTE, field: Path("relative/dir")})


def test_absolute_roots_are_kept_as_given() -> None:
    roots = JobRoots(**_ABSOLUTE)

    assert (roots.home, roots.repo, roots.user_home) == tuple(_ABSOLUTE.values())
