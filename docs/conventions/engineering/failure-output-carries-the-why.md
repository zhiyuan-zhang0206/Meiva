# A failure output carries the why

A human finding an error is a signal that the machine was not there. Two different errors reach us and
they need two different machines — neither of them is a new tool:

- **a behaviour error** (the code, or a check written for it, was wrong) ⇒ **one test**, whose failure
  output is the whole context a later reader gets: what it protects, why it exists, what breaks, where
  to read.
- **a process error** (the check existed and was green before; that layer simply did not run this
  time) ⇒ **a mandatory run**, never another test. A second test for a fact the first one already
  states changes nothing.

The core finding behind the second arm, before anything else, because it decides what to build:
**what was missing is not a mechanism. It is that `scripts/ci/test_selector.py` — which already maps a
change to the tests that must run — has to be run once before a merge.** Do not go and build a
detector for "signature changes": the selector's import closure already covers them, and the whole
thing exists today.

## The rule the user gave (2026-10-31)

> If we keep finding that a human is the one who discovers the errors, we should write what we found
> into the code as tests. And when such a test fails, the failure output has to explain the design and
> the motive behind it. We have to consider that an agent running the work can hardly account for what
> the human was thinking, because its world is only the code and the failure output.

Read it as one sentence per clause: a human-found error becomes a test; that test's failure output
carries the design and its motive; the reader is an agent that has the failure output and nothing else.

## Behaviour errors: one test whose failure output is the context

### The submission gate (three criteria, as the reviewer wrote them)

The reviewer's wording (`#3230`, 2026-10-31), rendered in English because the repository admits no raw
CJK outside frontend locales; the Chinese original is the reviewer's message of the same date, quoted
here as the authority, not a paraphrase of intent:

1. **A red proof**: the PR must carry a test that **fails on the code before the fix**. A test written
   alongside the implementation, green from birth, proves nothing — that is not verification, it is
   copying the implementation out again.
2. **The failure output carries the motive**: when that test fails, its output must say *why this
   line exists*. `assert 3 != 4` alone carries no motive. In an agent's world the failure output is
   its only context; a bare value cannot tell it whether to change the code or the assumption.
3. **When no test can be written, say why not**, and give substitute evidence (a manual reproduction,
   a log excerpt, a foreign system's screenshot).

### What the failure output must answer

Three questions, answered by the output alone — no repository browsing, no PR history:

1. **The behaviour**: one sentence naming what must hold.
2. **Why it exists**: the design motive or the incident, with a date and the fact that happened. "For
   safety" is not a motive. "An operator can `rm -rf` their working directory, so accepting an
   unreadable caller block hands out one unattributed execution per deletion (2026-10-31)" is.
3. **What breaks, and where to read**: the consequence, plus the path of the document, decision record
   or ruling that owns it.

pytest gives every assertion a message; use it. An assertion without a message fails criterion 2 even
when the assertion itself is right.

This is the same discipline as [the four things a lint message carries](../lint-messages.md), applied to
a test instead of a check: the fact, the reason, who owns the repair, where the rule is written. Two
differences — the reason carries the motive's date and the incident behind it, and a test that cannot
be written has to say so and hand over substitute evidence.

### Accept the test itself, not only the behaviour

Paste the failure output, alone, to someone who was not there — a human or an agent. If they cannot
say **what to fix** and **what not to touch**, the output is unfinished. Same discipline as reviewing a
criterion by watching it go red: a test whose red is unreadable is a test the next agent deletes.

### Do not turn it green by editing it

A motive-bearing test may only be weakened or deleted by someone who did not write it, with the reason
in the PR body. For the tests this document governs, the reason must say what replaced the
coverage — a rewrite, a gate, or nothing (and then why nothing).

### Worked example: the caller identity refusal

`cli/database/tests/test_lifecycle_audit.py::test_a_command_that_cannot_name_its_caller_is_refused`
(2026-10-31, `#7920`). Every assertion carries the fact and the consequence, and the test's note names
the ruling that owns the decision:

```python
assert child.returncode == 3, (
    "a lifecycle command that cannot read its caller block must exit 3 rather than run: "
    "that block is the only fact naming who asked, and an operator can remove it on "
    "purpose (a deleted working directory), so proceeding hands out one unattributed "
    f"execution per `rm -rf`. stderr={child.stderr!r}"
)
```

### Counter-example: the same day, one file over

`base/agents/history/tests/test_checkpoint_postgres_walks.py` (`#7919`) pins a real refusal — a
checkpoint-postgres release that changes the private surface must refuse at saver construction — but
stated it as `pytest.raises(RuntimeError, match="stage-2 row changed")`. Delete the refusal and the
report is `DID NOT RAISE <class 'RuntimeError'>`: no behaviour, no motive, no consequence.

`#7929` (`73d1b1eca`, 2026-10-31) rewrote those five to fail through a `_refusal(build, motive)`
helper: when the build succeeds instead of refusing, the failure text is the motive itself — what the
adapter reads from the dependency, why a change there surfaces as a mis-cut message reset far from its
cause, where the surface is owned, and what acceptance would mean. That rewrite is this convention's
first application, not an exception to it.

### When the behaviour cannot be asserted without breaking another rule

`base/cluster/postgres.py::boot_id` returns the boot scope a receipt is compared against. Until
2026-10-31 (`a979b0315`) it began with a platform read whose only job was to refuse an unsupported
system; the read was removed as a duplicate — `native_boot_id()` refuses there
(`base/native_process/kernel_identity.py`) — and the refusal therefore still holds, one layer down.

A test that pins it must simulate an unsupported platform. In this repository that means patching
`sys.platform` or the platform predicates, and the checker records both as debt
(`stdlib-patch` / `first-party-patch`) precisely because it wants the production code to take its
dependencies as parameters instead. The honest answer is criterion 3, with the substitute evidence
named here: `Platform.from_sys_platform` rejects anything outside the pair and is already tested
(`base/native_process/tests/test_os_platform.py`); the refusal itself is
`base/native_process/kernel_identity.py::native_boot_id`'s final branch; a receipt from another boot is
refused and that *is* covered (`base/cluster/tests/test_storage_ownership.py`). Adding a test seam to
`native_boot_id` is the production change that would make the refusal directly assertable — it is a
candidate, not something this document smuggles in.

## Process errors: a mandatory run, not a test

### The two kinds of "no test can be written"

- **the behaviour is hard to assert** (timing, a foreign system, non-determinism) ⇒ criterion 3:
  substitute evidence, named in the PR.
- **the check exists and did not run** ⇒ *not* criterion 3. The fix is a **mandatory-run set**: the
  group of checks that should have run is written down and made mechanical, so "present but unrun"
  stops being structurally possible. `#7910` (2026-10-31) is the specimen: seven red tests were in the
  tree the whole time; PR CI was off, so nothing ran them.

### Where the mandatory run lives

- **The mapper already exists.** `scripts/ci/test_selector.py` turns "which files changed" into "which
  tests must run" through the import closure. Run on `#7910`'s real change surface (22 files) it
  selected 645 tests, and those contained the three files that were missed
  (`cli/commands/agents/tests/test_relay_command.py` and `test_relay_connection_retry.py`). So the
  answer to a signature change is *run the selection*, never *build a detector for the shape*.
- **CI makes the run mandatory.** The pull-request checks in `ci.yml` run the selected tests and the
  whole-tree type check, and `main`'s branch protection requires them, so "present but unrun" stops
  being structurally possible. The release train re-runs the full suite on `main`.
- **A local run is optional and uses the same mapper.** `scripts/hooks/affected_tests.py` runs the
  selected files before a push (see [contributing](../../contributing.md#local-checks-before-a-push)).
  Everything else a local run might repeat (ruff, lints, `meizel`, changed-file BasedPyright) is
  already a pre-commit or pre-push hook.
- **A blind spot is never green.** When the selector reports `blind_changed` files, or cannot bound the
  change at all, its decision is `FULL`: CI runs the whole suite, and the local script prints
  `NOT RUN` and exits non-zero rather than reporting a pass for a selection it does not trust.
  BasedPyright, which resolves real types, closes the type-level blind spots the selector cannot see;
  the two complement each other and no third mechanism is needed.

### The retired receipt runner

`scripts/hooks/diff_checks.py` (landed in `#7230`, 2026-10-31) derived layers from a diff, ran
BasedPyright, ruff, `meizel` and pytest, and printed a `diff-checks/v1` receipt that the merge gate
required in the pull-request body. It was removed on 2026-11-26, together with the gate condition that
read the receipt:

- it repeated pre-commit, pre-push and CI, so a green receipt added no evidence CI did not already
  carry, and a hand-written receipt was the same offence as a hand-written test result;
- it started BasedPyright and several pytest workers at once, which exhausted the memory of a shared
  host when several agents ran it concurrently;
- its sub-check cache and scratch-directory ownership logic were a second system to keep correct next
  to the three that already judge the change.

The specification it was built against is in git history (`git show bdbccae228:docs/conventions/engineering/failure-output-carries-the-why.md`).

### One sentence

**The answer to a process error is not a new tool; it is making the existing tool mandatory before a
merge.** The selector was there all along; seeing a miss and going looking for a new mechanism is
exactly where the principle is easiest to get wrong.

## What this does not replace

An audit asks *whether something was deleted*.
This convention asks *whether the deletion was frozen into a test* (behaviour) or *into a mandatory
run* (process). A pull request can pass the gate and still owe both.
