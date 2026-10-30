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

A replacement is **not** a stand-in when it is the same name, defined by the same module, that the module serves right now — that is a module body running again in the dict the process already registered, which is what `base/packages/plugins/loading/plugin_loader.py` does by spec (its `#147` contract) and what `importlib.reload` does. A re-executed module rebinds every name it defines **and** every name it imports from a sibling face: `agent_runtime.FleetConfig` is `default_config`'s class, so a boundary that judged the second identity would red every shard that loaded the plugin faces twice (`#7832`, 2026-10-30 — the same day `#7827` landed, and not a revert of it: the check is unchanged, one exemption is). `gateway/tests/extensions/test_schedule_runner.py` had named the same-module half: the `meiva_to_user` plugin is re-executed in place.

Two blind spots follow, both accepted: a test that "replaces" a function with another function of the same module is not named, and neither is an object installed into both the module and the module that defines it. A stand-in whose definition lives elsewhere — a class the test file defines — carries that file as `__module__`, so it is still named; `tests/ci/test_leak_guard.py` pins that side on its own suite.

The finding names the **file**, and lands on the test that followed it (the first test of the next file in that worker), because a file boundary has no test at its teardown. In `fail` mode the original objects are put back before the next file's tests run, so no victim fails, and the run is red through the session's exit status instead of an innocent test. `tests/ci/test_leak_guard.py` locks both halves on a two-file suite, and the `#7796` mutation (the pre-`#7816` one-argument stand-in) is the case in the PR that added it.
