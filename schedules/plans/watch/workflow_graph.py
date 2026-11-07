# meiva:note
# The workflow's own dependency graph, read to answer one question: of the jobs that failed in a
# run, which ones only failed because another failing job did. `needs` is the definition, so the
# answer follows the code instead of a table of job names that would rot.
# meiva:link schedules/README.md

from __future__ import annotations

import re

import yaml

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of

__all__ = ["downstream_failing_jobs"]

# meiva:note
# Timing is the cheap proxy for "which job caused which" and it is wrong in the direction that costs
# a real failure: `needs: A` implies `start(B) >= end(A)`, but the converse does not hold, because
# two independent jobs share one small runner pool. In this repository it happens: on 2026-11-07
# run 42418213748 the four e2e shards started together, shards 1, 2 and 4 finished by 22:03:54, and
# shard 3 only started at 22:07:22 (five suites, four workers) -- and shard 3 was the red. A rule
# keyed on "started after another failing job ended" would have folded that red away.
#
# A name that matches no job, several jobs, or an unreadable document yields nothing to fold: the
# caller then reports every failure, which is the cheap direction to be wrong in.
_EXPRESSION = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)


def _display_pattern(template: str) -> re.Pattern[str]:
    parts = _EXPRESSION.split(template)
    return re.compile("^" + ".*".join(re.escape(part) for part in parts) + "$")


# meiva:note
# The names among `failing` whose transitive `needs` include another failing job: they started only
# because the graph let them, so their red is the upstream red seen once more.
def downstream_failing_jobs(workflow_yaml: str, failing: tuple[str, ...]) -> tuple[str, ...]:
    try:
        document = object_of(yaml.safe_load, workflow_yaml)
    except yaml.YAMLError:
        return ()
    if not is_object_dict(document):
        return ()
    jobs = document.get("jobs")
    if not is_object_dict(jobs):
        return ()
    needs: dict[str, tuple[str, ...]] = {}
    patterns: list[tuple[str, re.Pattern[str]]] = []
    for key, spec in jobs.items():
        if not is_object_dict(spec):
            continue
        raw = spec.get("needs")
        if isinstance(raw, str):
            needs[key] = (raw,)
        elif is_object_list(raw):
            needs[key] = tuple(one for one in raw if isinstance(one, str))
        else:
            needs[key] = ()
        name = spec.get("name")
        patterns.append((key, _display_pattern(name if isinstance(name, str) else key)))
    failing_keys = {key for name in failing for key, pattern in patterns if pattern.match(name)}
    if not failing_keys:
        return ()

    def ancestors(key: str) -> set[str]:
        seen: set[str] = set()
        stack = list(needs.get(key, ()))
        while stack:
            one = stack.pop()
            if one in seen:
                continue
            seen.add(one)
            stack.extend(needs.get(one, ()))
        return seen

    downstream: list[str] = []
    for name in failing:
        keys = [key for key, pattern in patterns if pattern.match(name)]
        if any(ancestors(key) & failing_keys for key in keys):
            downstream.append(name)
    return tuple(downstream)
