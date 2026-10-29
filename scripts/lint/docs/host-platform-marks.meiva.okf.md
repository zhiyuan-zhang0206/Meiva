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

`cli/commands/lifecycle/tests/start_storage/test_start_host_dependencies.py` carries the note
this rule generalises: a macOS host takes the launchctl rows even while the test
process runs on Linux, because the platform comes from `Host`, never from
`sys.platform`.

## Rule 1 — the process platform

`sys.platform`, `os.name`, `Host.system()`, `Platform.current()`, the tree's own
`is_macos()` / `is_linux()` predicates, and the six host calls of the standard
`platform` module — `platform.system()`, `platform.platform()`, `platform.uname()`,
`platform.release()`, `platform.mac_ver()`, `platform.win32_ver()` — read the
platform of the process that happens to run the test. The predicates are covered
because they reach the same read underneath: exempting them by name would leave
the fact one call deeper.

`platform` is a bounded module, so this list is finite rather than a snapshot.
`platform.python_version()` and `platform.python_implementation()` are **not**
covered: the interpreter version is not a host fact. `platform.machine()` is
**not** covered either: it answers the architecture, and no marker carries that
dimension, so counting it would only produce noise.

**The boundary:** a dynamically named read — `getattr(platform, "system")`,
`importlib.import_module("platform")`, an aliased `import platform as p` — is
outside this rule. Static analysis cannot see the name, and a boundary written
down is not the same thing as a gap discovered later. A test that needs one platform's behavior
injects a `Host` with that platform. When the subject itself only exists there —
launchd, TCC, codesign, swiftc — the test carries
`@pytest.mark.requires_host("darwin")`, which `tests/fixtures/host_platform.py`
turns into a skip on every other host, and a CI lane must run it on the named
host. The marker is a declaration the reader can see: "this test is about the
host", never "this test is hard to port".

## Rule 2 — the ambient environment

`shutil.which(name)` resolves through `os.environ["PATH"]` when it is called
without `path=`, and `pytest.importorskip(name)` skips the test when the module
is missing from the machine that runs it — in both cases the lane, not the test,
decides the verdict. Its remedy is not the marker: the test builds a `Host` whose
`which` returns the program, so it states what the machine has instead of asking
it. When the program belongs to a lane that provisions it (`uv`, `node`, a
database binary), the test states that with `# host-ok: <lane>`.

## Exemption

`host-ok: <reason>` on a comment inside a function's body, or at module level,
exempts that function or module from both rules. In a new or rewritten file the
reason travels inside a `meiva:note` block — the structured-comment rule
(`docs/conventions/engineering/structured-code-comments.md`) rejects free prose
there — so the exemption reads as a note that begins `host-ok:`.

A comment alone is a free pass: nobody reviews a line they do not expect. So the
exemption is also a record entry,
`scripts/lint/baseline/host_platform_exemptions.json`:

```json
{
  "allowed": 2,
  "exemptions": {
    "tests/fixtures/host_platform.py::_current_system": "this module IS the translation …"
  }
}
```

The key is `<path>::<function>` (`<module>` at module level); the value is the
reason exactly as the comment writes it, so a reviewer reads the justification in
the diff. Three things are checked, and each failure names the site: an exemption
that is not in the record, a recorded reason that no longer matches the comment,
and a reason that names neither a path in this repository nor a CI job (the job
ids come from `.github/workflows/ci.yml`). A record entry whose site is gone is
an error too — `allowed` is a frozen count, and it only falls when someone spends
the line to lower it. `--write-baseline` rewrites both records.

The marker is read through the tokenizer, so a fixture that writes
`# host-ok:` into a string literal is data, not a claim.

## Baseline

`scripts/lint/baseline/host_platform_marks.json` freezes what the rule already
finds as `read -> count` (`sys.platform`, `is_macos()`, `shutil.which(...)`, …).
The count follows the read, not the file: renaming or moving a test changes no
count, while every new read of a counted kind raises one and fails. A kind that
shrinks below its count fails too: the change that removes a read lowers the
record in the same diff, so a count never outlives the reads it was frozen from
(and no later branch inherits the debt of re-recording it). `--write-baseline`
regenerates the file after a deliberate repair. A `--only` run still compares
against the whole tree — otherwise a new read in the file under review would
pass locally and fail in CI. It then lists every site of the kind that grew, one
line each, and marks the ones outside the change with `[outside your change]`:
the count is the whole tree's, so the report can name a file the author never
opened, and saying which sites are theirs is the difference between a gate and a
mystery. The baseline
only shrinks; when the last entry goes, the file and this section go with it.

## Cost

The run is whole-tree, so its cost follows the corpus rather than the change: on
2026-10-29 the 3191 test files (21.3 MB) cost about 14s while the commit under
judgement touched one file. Two facts about the marker make that cheaper without
changing the verdict. Each file is parsed once — the findings and the exemptions
read the same tree — and a file whose text does not contain `# host-ok:` carries
no exemption at all, so the span scan and the tokenizer pass are skipped for it.
The second check is the marker's own text, not a guess at the spellings a reading
can take: every exemption is a comment token whose text is a substring of the
source, so its absence is the end of the question. The same run then costs about
6s, and what is left is the whole-tree walk itself.

## Scope

Every tracked file inside a pytest test host (from
`tool.pytest.ini_options.testpaths`), so a package's own `tests/` directory is
judged exactly like the top-level one. Production code branches on the platform
through `Host` by design; this rule is about tests.

Parent: [[scripts/lint/docs/lint.meiva.okf.md|lint]].
