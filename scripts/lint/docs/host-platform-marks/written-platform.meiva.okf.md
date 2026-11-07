---
type: doc
title: The platform written down
description: Why a test may not write down the platform a launch receipt names, the three definition positions the rule judges, the closed vocabulary, and the two boundaries it does not see.
tags:
- scripts
- lint
- testing
---

# The platform written down

The first two rules of [[scripts/lint/docs/host-platform-marks/host-platform-marks.meiva.okf.md|host-platform marks]]
catch a test that *reads* the machine. This one catches a test that *writes the answer down* — and
it takes a static rule because a platform literal is invisible on Linux by construction: `"linux"`
is what the Linux runner's `Platform.current()` says too, so no lane can redden on it. Only a
reader of the source sees the difference, and on 2026-11-08 that difference cost two changes the
same night (`#8659`, `#8660`): both were green in CI and red on macOS.

The value is a launch receipt's platform. `postgres.start` takes it as a `system=` argument, writes
it into the receipt, and `base/cluster/postgres.py::read_receipt` refuses a receipt whose platform
is not `Platform.current().system` — so a written-down value is unreadable on every host it does
not name.

## The four positions

All four are a *definition*, never a use:

- **A constant.** A module-level or class-level binding whose value is a platform name (`"linux"`,
  `"darwin"`) or a `Platform(...)` construction — `#8660`'s `NATIVE_TEST_SYSTEM = "linux"`, and the
  canned platforms `base/host/system/tests/fake_commands.py` builds for every test that picks one.
- **A default.** A parameter default that is one of those, because it is the value every caller
  that omits the argument inherits — `#8659`'s `platform: Platform = LINUX_X86` default on
  `fake_host`.
- **A bound name.** A name inside a test's body bound from a host's platform
  (`system = fake_host(uid=os.geteuid()).platform.system`), which is how one fixture wrote a Linux
  receipt that macOS then refused at setup. The attribute names are read from the tail, so the
  inline host and the fixture parameter are one finding; `sys.platform` and `os.name` are excluded
  — they are the process's own platform, which rule 1 already judges.
- **A host object's platform.** A `system=` argument whose value is read off a host (`.platform`,
  `.platform.system`). A host a test builds is a double, so its platform is the canned one:
  `#8659`'s failing line passed `command_runtime.host.platform.system` to a bring-up that wrote it
  into a receipt.

A platform a test *chooses* is written where it chooses it, so uses stay silent:
`fake_host(platform=DARWIN_ARM64)` is a test saying which host it wants, and the call is not a
claim about the machine running it. The remedy for a finding is therefore not "never name a
platform" but "name it at the point that picks it, and read the process's own where the process's
is meant".

## The closed vocabulary

`Platform.system` holds `linux` and `darwin` and refuses anything else, so `"macos"` or `"osx"`
never reaches a receipt. `scripts/lint/tests/test_lint_host_platform_marks.py` asserts this rule's
vocabulary equals the values `Platform`'s own annotation accepts, so a third system reddens that
test instead of slipping past the lint.

## Two boundaries

- A default spelled as a **name** (`platform: Platform = LINUX_X86`) is not reported at the default;
  the constant it names is the site that is judged, and that is where the remedy goes. Resolving
  names across modules would be a rule of its own.
- A platform that travels further than one binding is **invisible**: `host.platform.system` bound
  to `system` is judged, and `other = system` handed on from there is not. This rule judges the
  four positions above, not data flow — a known hole, not coverage, and the reason the daily macOS
  lane exists beside it.

Parent: [[scripts/lint/docs/host-platform-marks/host-platform-marks.meiva.okf.md|host-platform marks]].
