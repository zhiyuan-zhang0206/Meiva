from __future__ import annotations

import pytest

from scripts.host_ops import admission_floor

# meiva:link scripts/host_ops/docs/admission-run/disk-floor.meiva.okf.md


# meiva:note
# The pure half of the disk floor (task #5872): the verdict is a function of the readings and of
# the state the previous invocation left behind, never of a single sample.
class TestFloorVerdict:
    @pytest.mark.parametrize(
        ("readings", "refused_before", "expected"),
        [
            ([22.9], False, "warn"),
            ([22.9, 22.8, 22.9], False, "refuse"),
            ([22.9, 23.5, 23.5], False, "warn"),
            ([23.0, 23.0, 23.0], False, "warn"),
            ([23.5, 23.5, 23.5], True, "refuse"),
            ([24.5, 24.5, 24.5], True, "ok"),
            ([26.0], True, "ok"),
            ([22.0, 22.0, 22.0], True, "refuse"),
        ],
    )
    def test_the_verdict_is_the_readings_and_the_state(
        self, readings: list[float], refused_before: bool, expected: str
    ) -> None:
        assert (
            admission_floor.floor_verdict(
                readings,
                refuse_gib=23.0,
                warn_gib=24.0,
                reset_gib=24.5,
                refused_before=refused_before,
            )
            == expected
        )

    def test_leaving_the_refusal_below_the_warning_line_warns(self) -> None:
        assert (
            admission_floor.floor_verdict(
                [24.6], refuse_gib=23.0, warn_gib=25.0, reset_gib=24.5, refused_before=True
            )
            == "warn"
        )

    def test_a_zero_floor_is_always_ok(self) -> None:
        assert (
            admission_floor.floor_verdict(
                [0.0, 0.0, 0.0],
                refuse_gib=0.0,
                warn_gib=0.0,
                reset_gib=1.5,
                refused_before=True,
            )
            == "ok"
        )

    # meiva:note
    # A reader that stops answering ends the probe with fewer samples, and fewer samples can
    # warn but never refuse: a flaky probe must not block work.
    def test_a_short_probe_cannot_refuse(self) -> None:
        assert (
            admission_floor.floor_verdict(
                [22.0, 22.0],
                refuse_gib=23.0,
                warn_gib=24.0,
                reset_gib=24.5,
                refused_before=False,
            )
            == "warn"
        )
