---
type: doc
title: Host-platform marks
description: Why a test injects a Host instead of reading the machine it runs on, the two rules the lint applies, and how the frozen baseline shrinks.
tags:
- scripts
- lint
- testing
---

# Host-platform marks

`Host` is this repository's port to the machine: a component branches on
`host.platform` and resolves programs through `host.which`, so a test builds the
host it wants (`base/host/system/tests/fake_commands.py:fake_host`) and asserts
the same result everywhere. Reading the *process's* own platform or PATH inside a
test does the opposite — the verdict becomes a property of the machine that runs
it, so the test is green in CI and red on a developer's macOS, or the reverse,
which is worse because CI is the one that reports.

`cli/commands/lifecycle/tests/test_start_host_dependencies.py` carries the note
this rule generalises: a macOS host takes the launchctl rows even while the test
process runs on Linux, because the platform comes from `Host`, never from
`sys.platform`.

## Rule 1 — the process platform

`sys.platform`, `Host.system()` and `Platform.current()` read the platform of the
process that happens to run the test. A test that needs one platform's behavior
injects a `Host` with that platform. When the subject itself only exists there —
launchd, TCC, codesign, swiftc — the test carries
`@pytest.mark.requires_host("darwin")`, which `tests/fixtures/host_platform.py`
turns into a skip on every other host, and a CI lane must run it on the named
host. The marker is a declaration the reader can see: "this test is about the
host", never "this test is hard to port".

## Rule 2 — the ambient PATH

`shutil.which(name)` resolves through `os.environ["PATH"]` when it is called
without `path=`. Its remedy is not the marker: the test builds a `Host` whose
`which` returns the program, so it states what the machine has instead of asking
it. When the program belongs to a lane that provisions it (`uv`, `node`, a
database binary), the test states that with `# host-ok: <lane>`.

## Exemption

`host-ok: <reason>` on a line inside a function's body, or on a module-level
line, exempts that function or line from both rules. In a new or rewritten file
the reason travels inside a `meiva:note` block — the structured-comment rule
(`docs/conventions/engineering/structured-code-comments.md`) rejects free prose
there — so the exemption reads as a note that begins `host-ok:`. The reason is
for the next reader, and a reviewer decides whether it holds;
`tests/fixtures/host_platform.py` and the test that locks it use it because
asking the current host *is* their subject.

## Baseline

`scripts/lint/baseline/host_platform_marks.json` freezes the sites the rule
already finds as `path -> count`. A file may shrink below its count but never
grow past it, and an entry for a file that no longer exists is an error:
`--write-baseline` regenerates the file after a deliberate repair. The baseline
only shrinks; when the last entry goes, the file and this section go with it.

## Scope

Every tracked file inside a pytest test host (from
`tool.pytest.ini_options.testpaths`), so a package's own `tests/` directory is
judged exactly like the top-level one. Production code branches on the platform
through `Host` by design; this rule is about tests.

Parent: [[scripts/lint/docs/lint.meiva.okf.md|lint]].
