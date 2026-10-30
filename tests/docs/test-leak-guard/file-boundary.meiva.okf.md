---
type: doc
title: "Leak guard — the file boundary"
description: "The comparison that names a file which leaves an existing callable of a first-party module pointing at another object, and why it is per file rather than per test."
tags:
- evaluation
- quality-assurance
---

# Leak guard — the file boundary

Roster: [[test-leak-guard.meiva.okf.md]].

`module-attr` compares dict sizes, so it cannot see an existing name handed a different object: the size does not move. `module-attr-replaced` closes that hole with a comparison per FILE, not per test — two bulk passes over the callables of the dicts the watch already located, taken when a worker moves to another file and at session end. A per-test scan would cost a shard its own test time; per file it is about 10 ms, and a shard runs a few hundred files.

What it holds to is the module's **callables**: a function or class handed to a module is a stand-in every later reader calls. A plain object (the bound agent identity among them) is per-test state with its own owner, and `identity_restore` already puts that slot back after every test.

A replacement whose defining module is the module being compared is **not** a stand-in: it is the module's own body running again in the same dict (a plugin loader executing its spec a second time, `importlib.reload`), which rebinds every callable it defines. `gateway/tests/extensions/test_schedule_runner.py` is the case that named it: the `meiva_to_user` plugin is re-executed in place, and a boundary that judged it would red every gateway shard. A blind spot follows — a test that "replaces" a function with another function of the same module is not named.

The finding names the **file**, and lands on the test that followed it (the first test of the next file in that worker), because a file boundary has no test at its teardown. In `fail` mode the original objects are put back before the next file's tests run, so no victim fails, and the run is red through the session's exit status instead of an innocent test. `tests/ci/test_leak_guard.py` locks both halves on a two-file suite, and the `#7796` mutation (the pre-`#7816` one-argument stand-in) is the case in the PR that added it.
