# meiva:note
# Grouping cadence from the values captured at one decision point.


def check_threshold(level: int, *, check_open: int, check_decay: int, minimum: int) -> int:
    # meiva:note
    # The per-level cadence, rounded as before and bounded by its minimum. The power is a float
    # power because the type stubs give `int ** int` the type `Any`.
    return max(minimum, round(check_open / float(check_decay) ** (level - 1)))
