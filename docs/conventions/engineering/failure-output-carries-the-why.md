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
in the PR body. The mechanism exists already: the `diff hints` gate asks a deleted test for a
`Hint-Ack: deleted-test <path>:<line> -- <reason>` trailer. For the tests this document governs, the
reason must say what replaced the coverage — a rewrite, a gate, or nothing (and then why nothing).

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

### The checklist

The body of this arm is `#7230`'s checklist (2026-10-31), rendered in English here because of the same
no-CJK rule; the checklist's own text is the authority and its structure, numbers and decisions are
reproduced, not summarised.

**Status (2026-10-31 19:44)**: the runner and its receipt checker are in the tree —
`scripts/hooks/diff_checks.py` and `scripts/hooks/diff_checks_receipt.py`, landed by `#7230`. This
section is the specification they were built against; where the build moved a decision (the location,
the refusal-loss red rule, the type-check attribution), the text says so in place. The manual gate
under "The refusal-loss trigger" is superseded by the `debt-reduced` trigger and retires with it.

#### 0. The finding: the mapper exists, what is missing is somebody running it

`scripts/ci/test_selector.py` already turns "which files changed" into "which tests must run" through
the import closure. Run on `#7910`'s real change surface (22 files) it selected 645 tests, and those
**contain the three files that were missed**:

```
cli/commands/agents/tests/test_relay_command.py
cli/commands/agents/tests/test_relay_connection_retry.py
tests/integration/test_impersonation_notifications.py      (decision=SELECTED, 645 tests)
```

So: do not write a detector for signature changes. Selecting by "who imports this module" is more
conservative than a signature or AST check. The real blind spots (dynamic imports, `**kwargs`
forwarding, non-literal imports) are reported by the selector itself through `blind_changed` /
`diagnostics`, and what closes those is a full-tree `basedpyright` (real type resolution). The two
complement each other; no third mechanism.

#### 1. Layers and costs (ubuntu, measured 2026-10-31)

| layer | command | cost | catches | whose job is the blind spot |
|---|---|---|---|---|
| **L0** full-tree static | `.venv/bin/basedpyright` (whole tree) | 3m13s | missing required arguments, cross-layer types, call sites behind `**kwargs`/dynamic imports | — |
| **L0'** format | `ruff check` + `ruff format --check` | seconds | anything red at commit | — |
| **L1–L3** impact tests | `python scripts/ci/test_selector.py --changed-files <diff> --base-ref origin/main --json`, then run its `tests` | with the surface (645 this time) | cross-layer construction (CLI tests), integration layer, domain unit tests | types: L0; selector: the blind-spot loop in §4.5 |
| **L3'** frontend | `npm test` (vitest, when `ui/web` changed) | minutes | frontend unit tests | visual is not in the mandatory set (§2) |
| **L4** repository gates | `python -m meizel check`; `python -m meizel debt diff --against origin/main --on-growth warn` | 20s–3min | manifest/fact drift, a mis-written ledger | — |

**L3 takes vitest only, never visual** (405): visual needs a browser, is screenshot comparison by
nature and is flaky by nature; putting it in the mandatory set teaches everyone to ignore a red visual.
Visual belongs to the ten-PR checkpoint's full run.

**L2 is the contract call surface**: the layer where a signature changed in one package meets a caller
in another. Two specimens from 2026-10-31, both red on `main` with nothing running them —
`cli/commands/observability/lgtm.py` (`backend_urls()` called with no argument after `4f3cac513` gave
it one; fixed by `#7932`) and `tests/integration/test_impersonation_notifications.py` (the only test
that constructs the relay command with the argument `#7910` added). Both are ordinary L1–L3
selections. That is the point: the selector already includes them, so the answer is *run it*, never
*detect the shape*.

#### 2. Where it lives: `scripts/hooks/diff_checks.py`, not inside meizel

The runner is named for what it decides — which checks this diff owes — and deliberately **not**
`preflight`: the repository already has a `cli/preflight.py`, the guard that decides which checkout may
operate on a home on every `meiva` invocation. One name for two jobs invites the wrong reader (405,
2026-10-31).

- **Why not inside meizel**: meizel is a static checker (it reads the tree); the runner is an
  orchestrator (derive layers → call pyright/ruff/pytest/meizel). Putting it inside meizel mixes two
  jobs, and meizel is itself called by the runner (L4) — a cycle.
- **Why `scripts/hooks/` and not `scripts/ci/`** (moved while building, 2026-10-31): two gates decide
  this. `scripts/ci` may not reach `scripts/audit/refusal_loss.py` — `scripts/audit` already reaches
  `scripts/ci` through `impact_census.py`, so that edge closes a cycle `meizel` refuses; and
  `scripts/ci` already holds its twenty direct entries, the cap `lint-code-structure` enforces.
  `scripts/hooks` is the pre-push family the runner unifies (`prepush-pyright-files.sh`,
  `prepush-meizel.sh`), so the location the gates force is also the one that reads right.
- **Hard constraint**: the runner depends only on meizel's *command-line contract* (`check`,
  `debt diff` output and exit codes) and never imports `meizel.*` internals; a change inside meizel
  must not be able to break the runner.
- Same family as the pre-push hooks it replaces; anyone can read it.

#### 3. The receipt (versioned, machine-checked)

One line, in the PR body:

```
diff-checks/v1 @<head-sha> base=<base-sha> pyright=0err(1m28s) err_in_this_diff=0 ruff=ok(0s) selector=SELECTED:152 tests=RAN passed=3617(6m02s) failed=0 fallback=none blind=none vitest=none next-build=none meizel=ok(exit=0,772manifests,76697edges)(17s) debt=ambient-read96->96,total201->201 refusal-loss=none skipped=none generated=scripts/hooks/diff_checks.py
```

`debt=` gives both the absolute numbers and the delta (405): the absolute numbers are what shows drift
against main in a receipt pinned to a sha; the delta only says what this change did. With one half
missing, every reader has to recompute.

**A checker beside it** (`scripts/hooks/diff_checks_receipt.py`, named for the receipt it reads)
compares three things: the
`sha` equals the PR head; the fields are all present
(`pyright/ruff/selector/passed/vitest/meizel/debt/refusal-loss/skipped/generated`); and every layer
named in `skipped` carries a reason — **the checker does not judge whether the reason is good**, which
would become subjective and teach people to pad reasons.

**What it stops**: a stale receipt (sha mismatch), a missing field, a skipped layer without a reason.
**What it does not stop**: a hand-written fake receipt. Hence the `generated=` field and the fixed
`diff-checks/v1` literal, and one sentence in this convention: **a hand-written receipt is the same
offence as a hand-written test result.**

#### 4. The trigger table (what changed ⇒ what must run)

| the change | must run |
|---|---|
| any `.py` | L0 + L0' + L1–L3 (selector) + L4 |
| a parameter added without a default / deleted / renamed / a return type changed | the same — **no extra detection needed**: the selector picks by import closure and covers cross-layer construction sites by construction |
| migration / schema | all of the above + the integration layer against a real Postgres (migration smoke) |
| gateway routes, request handling, configuration value objects | all of the above (the selector pulls `gateway/tests`) + that domain's e2e |
| configuration fields / `meizel.toml` | L4 (including `meizel sync --prune` when needed) + the touched domain's L1 |
| `ui/web/**` | additionally `vitest` |
| documentation or comments only | L4 + the link checks |

#### 4.5 Closing the blind spots (405, hard rule)

The selector's blind spots **must appear in the receipt, and may never be counted as green** — this is
stronger than "run full-tree pyright"; they are not the same thing: pyright closes *type-level* blind
spots, `blind_changed` names *selector-level* ones (dynamic imports, non-literal imports — edges the
selector cannot see at all).

- **`blind_changed` non-empty** ⇒ the receipt says `selector=BLIND(<files>)`, and the fallback layer
  (that domain's full suite, or the whole suite) **must** run; the receipt records `fallback=RAN(<scope>)`.
- **No fallback ⇒ the PR cannot merge.** Not because the author was lazy, but because **the selector
  itself said it cannot see**.
- `diagnostics` is handled separately (the whole repository has **34** of them today; "non-empty ⇒ full
  run" would make every PR run everything and thereby defeat itself): the receipt records
  `diagnostics=<n>`, and **one diagnostic pointing at a file this change touches** carries the same
  weight as `blind_changed` — the fallback must run.
- **A ratchet on `diagnostics` (405)**: the count may only fall. **34 is a ceiling, not a starting
  point**; a new one requires two statements in the PR body — why this dependency edge is invisible,
  and why it cannot be removed. Each one means "the selector is blind to this file's edge", and nobody
  notices when 34 quietly becomes 60.

#### 5. Making people actually run it (without relying on discipline)

1. `python scripts/hooks/diff_checks.py` derives the layers from `git diff --name-only origin/main...HEAD`, runs
   them and prints the receipt; `--dry-run` prints what would run, at zero cost.
2. **The receipt goes into the PR body and a merger accepts only PRs that carry one**; skipped layers
   need a reason, and **a skip is not green**.
3. Optional second phase: put L0 + L4 into the existing pre-push hook (currently 4–5 minutes) so a red
   refuses the push; L1–L3 are too expensive and stay in the runner.

#### 6. The expiry rule (what happens when CI comes back)

- **ci-mode is machine-decidable**: the runner reads the PR head sha's check-runs
  (`gh api .../commits/<sha>/check-runs`); empty ⇒ `ci-mode=off`, and the receipt says "this run was
  mandatory because ci-mode=off".
- **When CI returns, every layer with an equivalent CI job degrades out of the mandatory set** (L0
  pyright, L0' ruff, L1–L3 pytest, L3' vitest); the runner survives as an optional accelerator (a result
  in five local minutes versus a CI round trip).
- **Three families stay mandatory** because CI does not cover them structurally, or covers them more
  narrowly: the fallback when `blind_changed` is non-empty (CI is blind in the same place); the real
  resource integration layers behind a skip (with no local database they must be named in `skipped=`,
  never left empty); and the ledger's **whole-repository** reading (`debt diff`) — CI runs only
  `meizel-check-changed`.
- The expiry rule itself must be reviewed: on the merge that restores CI, the merger checks this table
  and updates this document.

#### 7. Today's evidence

- **`#7910`**: on its change surface the selector picked 645 tests, including the three files that were
  missed; full-tree pyright listed the 7 missing arguments in 3m13s. Neither ran ⇒ main went red. This
  time `blind_changed=[]` and `diagnostics=34` (all pre-existing, none pointing at this change), i.e.
  the fallback was not needed — running the selector would have been enough.
- **The ledger**: without `debt prune` after a change, `meizel check` reports only `[debt-paid]` while
  `debt diff` still shows +0; after the prune it is −2. Skipping L4 writes a wrong ledger into the PR
  body.

#### 8. Decided (405, 2026-10-31 16:34)

- The runner is `scripts/hooks/diff_checks.py` and the receipt is `diff-checks/v1` (405, 16:50: the
  old name `scripts/preflight.py` collides with `cli/preflight.py`, the home-ownership guard). The
  checker sits beside it; the location moved from `scripts/ci/` for the two gates named in §2.
- `debt=` carries both the absolute numbers and the delta.
- The blind-spot loop is §4.5.

#### 9. One sentence

**The answer to a process error is not a new tool; it is making the existing tool mandatory before a
merge.** `scripts/ci/test_selector.py` has been there all along, and run on `#7910`'s real change
surface it selects 645 tests including the three files that were missed — this is exactly where the
principle is easiest to get wrong: seeing a miss and going looking for a new mechanism.

### The refusal-loss trigger (a net-debt-reducing change)

This is `#3230`'s gate of 2026-10-31 16:49, rendered in English for the no-CJK rule; the ruling is the
authority, this is its text.

A pull request **reduces recorded debt** when a `*meizel.toml` diff deletes more lines than it adds.
Such a pull request puts one line in its body, produced by a **single** scan of the tool:

```
refusal-loss: hits=N blocking=M unreplaced=K
```

**A hand-written approximation is not accepted** — including one "derived from the tool's output". The
line has to be the stdout of `refusal_loss.py --receipt`, verbatim: the three counts come from one
scan, and a reader who recomposes them by hand is back to typing a number that looks right. When
`blocking > 0`, every hit carries its review conclusion after that line.

The reviewer runs it themselves rather than trusting the pasted line — and the command is written out
because a missing `PYTHONPATH` does not fail: it answers, plausibly, from the wrong tree:

```bash
git -C ~/.meiva/source worktree add --detach /tmp/mv-main origin/main
cd /tmp/mv-main && PYTHONPATH=/tmp/mv-main ~/.meiva/source/.venv/bin/python \
    -m scripts.audit.refusal_loss --receipt --range <base>..<head>
git -C ~/.meiva/source worktree remove --force /tmp/mv-main
```

**One state now, and the one it is going to leave (2026-10-31 16:58):**

1. `--receipt` is live: `#7933` (`ab0faea1c`) prints exactly that line and exits non-zero when
   `blocking > 0`. The line is therefore the command's own stdout today, not a target it will meet.
2. The gate's **location** is temporary on purpose: it belongs in the diff-checks receipt above (field
   `refusal-loss=hits/blocking/unreplaced`) and moves there when the runner lands — one place, never
   two. 405 withdraws this manual criterion when it does.

The trigger exists at all because the tool blocks only one of the shapes a real deletion takes; the
other two ("a refusal deleted and something else added") are **listed, not blocked**, and a list
nobody reads is not a list — today proved it once already: `scripts/ci/test_selector.py` has been in
the repository all along, and `#7910` still slipped through, because nobody ran it.

## What this does not replace

The refusal-loss audit (`scripts/audit/refusal_loss.py`, `#7926`) asks *whether something was deleted*.
This convention asks *whether the deletion was frozen into a test* (behaviour) or *into a mandatory
run* (process). A pull request can pass the gate and still owe both.
