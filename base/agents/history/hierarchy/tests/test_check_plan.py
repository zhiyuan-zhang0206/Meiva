import pytest

from base.agents.history.hierarchy.check_plan import check_threshold


@pytest.mark.parametrize(
    ("base", "decay", "levels", "expected"),
    [
        (60, 3, (1, 2, 3, 4, 5, 9), [60, 20, 7, 6, 6, 6]),
        (90, 3, (1, 2, 3, 4), [90, 30, 10, 6]),
        (90, 1, (1, 5), [90, 90]),
    ],
)
def test_threshold_keeps_rounding_decay_and_floor(
    base: int, decay: int, levels: tuple[int, ...], expected: list[int]
) -> None:
    assert [
        check_threshold(level, check_open=base, check_decay=decay, minimum=6) for level in levels
    ] == expected
