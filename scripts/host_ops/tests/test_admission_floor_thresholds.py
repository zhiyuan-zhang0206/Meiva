from __future__ import annotations

from scripts.host_ops.admission_floor import (
    DEFAULT_DISK_REFUSE_GIB,
    DEFAULT_DISK_RESET_MARGIN_GIB,
    DEFAULT_DISK_WARN_GIB,
    DISK_QUANTUM_GIB,
)


# meiva:note
# The three numbers are one design, not three knobs: macOS moves free space in whole swapfile
# steps, so a gap narrower than two steps cannot separate two verdicts from one reading. On
# 2026-11-02 the warning sat one step above the refusal and the same host reported both within a
# minute; moving it to three steps fixed the reading, and this test keeps the next edit from
# quietly undoing it.
def test_the_two_lines_are_at_least_two_quantum_steps_apart() -> None:
    gap = DEFAULT_DISK_WARN_GIB - DEFAULT_DISK_REFUSE_GIB

    assert gap >= 2 * DISK_QUANTUM_GIB, (
        f"warn ({DEFAULT_DISK_WARN_GIB}) - refuse ({DEFAULT_DISK_REFUSE_GIB}) = {gap} GiB, "
        f"inside the {DISK_QUANTUM_GIB} GiB step the signal moves by"
    )


def test_the_reset_margin_is_wider_than_one_quantum_step() -> None:
    assert DEFAULT_DISK_RESET_MARGIN_GIB > DISK_QUANTUM_GIB, (
        f"reset margin {DEFAULT_DISK_RESET_MARGIN_GIB} GiB must exceed the "
        f"{DISK_QUANTUM_GIB} GiB step, or leaving the refusal band re-enters it on the next sample"
    )
