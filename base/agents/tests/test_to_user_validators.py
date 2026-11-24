import pytest

from base.agents.to_user import (
    FileValidators,
    file_validators,
    not_modified,
    range_applies,
    validator_headers,
)

_MTIME_NS = 1_732_494_000_123_456_789


def test_validators_come_from_size_and_modification_time_alone() -> None:
    validators = file_validators(5_669_036, _MTIME_NS)
    assert validators == file_validators(5_669_036, _MTIME_NS)
    assert validators.etag == f'"{5_669_036:x}-{_MTIME_NS:x}"'
    assert validators.last_modified == "Mon, 25 Nov 2024 00:20:00 GMT"
    assert validator_headers(validators) == {
        "ETag": validators.etag,
        "Last-Modified": validators.last_modified,
    }


@pytest.mark.parametrize(
    ("size", "mtime_ns"),
    [(5_669_037, _MTIME_NS), (5_669_036, _MTIME_NS + 1)],
    ids=["size", "mtime-nanosecond"],
)
def test_the_tag_changes_with_either_input(size: int, mtime_ns: int) -> None:
    assert file_validators(size, mtime_ns).etag != file_validators(5_669_036, _MTIME_NS).etag


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, False),
        ("", False),
        ('"stale"', False),
        ('"a", "b"', False),
        ("*", True),
        ('"{etag}"', False),
        ("{etag}", True),
        ("W/{etag}", True),
        ('"other", {etag}', True),
        ('{etag} , W/"other"', True),
    ],
)
def test_if_none_match_compares_weakly_and_accepts_lists_and_a_star(
    header: str | None, expected: bool
) -> None:
    validators = file_validators(10, _MTIME_NS)
    offered = None if header is None else header.replace("{etag}", validators.etag)
    assert not_modified(validators, offered) is expected


def test_if_range_keeps_a_slice_only_for_the_exact_strong_tag() -> None:
    validators = file_validators(10, _MTIME_NS)
    assert range_applies(validators, None)
    assert range_applies(validators, f"  {validators.etag} ")
    for offered in (
        "",
        file_validators(10, _MTIME_NS + 1).etag,
        f"W/{validators.etag}",
        validators.last_modified,
    ):
        assert not range_applies(validators, offered), offered


def test_validators_are_a_plain_value() -> None:
    assert FileValidators("a", "b") == FileValidators("a", "b")
