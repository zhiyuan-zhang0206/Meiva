# meiva:note
# Why an agent turn ended — the closed vocabulary behind `turn_end.ended_with`.
#
# The value is produced where the turn's fate is decided and carried to the record; it is
# never inferred at the emit site. Two values are the loop's own decision:
#
# - `no_input` — claim ended the turn at its idle boundary: nothing more arrived;
# - `budget` — a budget rail ended it: a truncated stream or an exhausted retry budget.
#
# The agent declares the other three during the turn, because only it knows them: `done`
# (the work is finished), `waiting:<object>` (a named object — a person, a CI run, an
# external event — has to answer first; the object is required) and `deferred:time` (the
# work was pushed to the clock; the declaration must carry a one-sentence reason).
#
# A value outside that set is an invalid record, not free text: `validate` refuses it where
# a declaration enters the turn's state, and the loop itself only ever writes these
# constants.
# meiva:link agent/graph/llm/docs/turn-records.meiva.okf.md

from __future__ import annotations

NO_INPUT = "no_input"
DONE = "done"
BUDGET = "budget"
DEFERRED_TIME = "deferred:time"
WAITING_PREFIX = "waiting:"

_VALUES = (NO_INPUT, DONE, BUDGET, DEFERRED_TIME, WAITING_PREFIX)


# meiva:note
# Return `value` as one of the five turn-end values, or raise ValueError. Called at the
# boundary where a declaration enters the state, so an out-of-set value never reaches a
# record. The paired `reason` is part of the contract: `deferred:time` is the
# dependency-free delay that has to be attributable, so it cannot be recorded without one.
def validate(value: object, *, reason: object = None) -> str:
    if not isinstance(value, str) or not value.startswith(_VALUES):
        raise ValueError(
            f"ended_with must be one of {NO_INPUT!r}, {DONE!r}, {BUDGET!r}, "
            f"{DEFERRED_TIME!r}, or {WAITING_PREFIX!r} plus the object it waits on "
            f"(e.g. 'waiting:ci:8341'); got {value!r}"
        )
    if value == WAITING_PREFIX:
        raise ValueError(
            "ended_with 'waiting:' needs the object it waits on after the colon "
            "(a person, a CI run, an external event), e.g. 'waiting:peer:6107'"
        )
    if value == DEFERRED_TIME and (not isinstance(reason, str) or not reason.strip()):
        raise ValueError(
            "ended_with 'deferred:time' needs a one-sentence reason for pushing the work "
            "to the clock; pass it as ended_with_reason"
        )
    return value
