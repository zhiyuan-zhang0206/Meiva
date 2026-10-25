# meiva:note
# Grouping cadence from the values captured at one decision point.


def check_threshold(level: int, *, check_open: int, check_decay: int, minimum: int) -> int:
    # meiva:note
    # The per-level cadence, rounded as before and bounded by its minimum.
    return max(minimum, round(check_open / check_decay ** (level - 1)))
