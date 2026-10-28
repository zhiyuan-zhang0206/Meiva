# meiva:note
# How loud one meter's own failed accounting is: the first failure and every
# FAILURE_REPORT_EVERY-th after it, so a seam that fails on every call stays visible in the
# log without flooding it.

from __future__ import annotations

FAILURE_REPORT_EVERY = 50


# meiva:note
# Whether the `failures`-th failed accounting of one meter is reported.
def should_report(failures: int) -> bool:
    return failures == 1 or failures % FAILURE_REPORT_EVERY == 0
