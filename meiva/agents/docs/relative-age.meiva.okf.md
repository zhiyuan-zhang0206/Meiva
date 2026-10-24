---
type: doc
title: Agent and open-task relative ages
description: Relative ages of agent rows and open tasks measure UTC elapsed time from explicit aware timestamps.
tags:
- sdk
- agent-lifecycle
---

# Agent and open-task relative ages

`AgentRow.__str__` shows the spawn age and, when different, the last-active age.
`OpenTaskRow.__str__` shows the age of its last update. Labels, status, machine,
field order and separators remain part of those existing representations.

The SDK facade obtains the current Clock and converts both timestamps through
its zone, retaining the existing interpretation of a naive input. A bound context
without a Clock still fails instead of borrowing an installation Clock. Each
relative-age call captures the real time once; rendering a row or page does not
promise a shared snapshot across its fields.

The internal `row_text.relative_age(moment, *, now)` formatter takes two aware
values and measures elapsed time after converting both to UTC. Spring jumps and
fall folds therefore do not add or remove elapsed hours. A naive internal input
is refused; it never selects an implicit local timezone.

Elapsed seconds truncate toward zero. Values below 60 seconds use `s ago`, then
whole minutes below 60 use `m ago`, whole hours below 24 use `h ago`, and whole
days use `d ago`. Future values keep the existing negative-seconds representation;
they are not clamped or reworded. The formatter reads no SDK configuration or clock.
