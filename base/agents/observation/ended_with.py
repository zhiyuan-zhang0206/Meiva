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
# A value outside that set is an invalid record, not free text. A declaration the record
# cannot use is recorded as `invalid` — distinct from a turn that declared nothing, whose
# field is simply absent — and it never aborts the turn. That is a deliberate exception to
# the repository's fail-fast rule (AGENTS.md, unknown enum values): a field that measures
# the agent must not fail the work it measures, or the instrument turns into a fuse and the
# measured failure rate becomes a function of the model's phrasing. `problem` says why a
# declaration was refused; the exec boundary records the marker and reports the rejected
# text as `turn_end_declaration_rejected`.
# meiva:link agent/graph/llm/docs/turn-records.meiva.okf.md

from __future__ import annotations

NO_INPUT = "no_input"
DONE = "done"
BUDGET = "budget"
DEFERRED_TIME = "deferred:time"
WAITING_PREFIX = "waiting:"
INVALID = "invalid"

_VALUES = (NO_INPUT, DONE, BUDGET, DEFERRED_TIME, WAITING_PREFIX)


# meiva:note
# Why `value` is not a usable declaration, or None when it is one. Called where a
# declaration enters the turn's state; a refusal is recorded (`INVALID`) and reported, never
# raised, so a rejected declaration costs the turn nothing. The paired `reason` is part of
# the contract: `deferred:time` is the dependency-free delay that has to be attributable, so
# it cannot be recorded without one.
def problem(value: object, *, reason: object = None) -> str | None:
    if not isinstance(value, str) or not value.startswith(_VALUES):
        return (
            f"not one of the turn-end values: {value!r}; accepted are {NO_INPUT}, {DONE}, "
            f"{BUDGET}, {DEFERRED_TIME}, and {WAITING_PREFIX}<object>"
        )
    if value == WAITING_PREFIX:
        return (
            "names no object to wait on: write 'waiting:<object>' — the person, CI run or "
            "external event that has to answer first"
        )
    if value == DEFERRED_TIME and (not isinstance(reason, str) or not reason.strip()):
        return "carries no reason: pass ended_with_reason, one sentence"
    return None
