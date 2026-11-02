# Backend test selection

## Purpose

PR CI runs the selected subset whenever impact is computable; the full suite
runs after the merge, on `main`, in the six-hourly Release Train and in the
nightly verdict.

Backend CI has one layer. A pull request runs the full suite only when its
impact cannot be computed (a global path, an unmapped path, a deleted path
without base facts) and otherwise, when the workflow is in enforce mode (the
default), an owner-rule-selected subset in its place. Nothing re-runs the full
suite on the tree a pull request would create: the Trunk merge queue that did
was retired on 2026-11-01
([decision](../../decisions/engineering/tooling/2026-11-01-release-train-and-channels.md)).
A gap in the PR-side subset therefore reaches `main`, and the post-merge runs
find it afterwards. Test selection does change broken-main risk, in one
direction and by a bounded amount; [what verifies a merge](#what-verifies-a-merge)
states exactly how much.

No threshold on the subset's size or estimated time sends a pull request back to
the full suite. "The impact is too large" has no definition that survives
contact with the repository (a fixture or plugin that most tests import makes
most of the suite the true impact, and no share of the suite separates that from
a harmless subset), and a large subset is still the cheaper of the two jobs.
Dropping the threshold is a deliberate trade against the concern of task #4183
(PR #3020's subset passed while the full population was red on
`tests/contracts/test_lint_event_kinds.py`): that concern is answered by the
pinned tree-scan tests (below) and by the post-merge full runs, not by sending
large subsets back to the full fan-out.

The selector is [scripts/ci/test_selector.py](../../../scripts/ci/test_selector.py). It is
stdlib-only and consumes the shared runtime dependency facts in
`scripts.structure.imports.facts`. It follows imports transitively through application
modules, test helpers and imported tests, and adds the actual pytest fixture bindings:
nested conftests, literal `pytest_plugins` of any conftest below the root, and the shared
`path_scopes.toml` reader. The root `conftest.py`, its `pytest_plugins` modules and
`tests/fixtures/` are global paths and add no per-test edge (see the harness below).
Each path-scoped binding retains its TOML source as an input, including removed base
declarations; a missing first-party fixture module reports incomplete impact at that source.
Relative imports, finite dynamic imports, literal Python subprocess modules/code and
recognized repository-rooted resource paths use that same evidence owner and module resolver.
Finite dynamic imports include the module's own `__name__` and subscripts of, or loops
over, an unmutated literal table bound in the same module. Literal file anchors include
`with_name`/`with_suffix` siblings; proven literal absolute reads outside the checkout
(such as `/proc/123/stat` or `os.devnull`) and write-only `open` modes are not repository inputs.
A fixed external prefix followed by an opaque segment stays unknown: absolute segments
can reset a joined path, and parent traversal can enter the checkout. Placement
subject policies do not prune this runtime impact graph. The selector never imports
application code or executes test code.

Fixture dependencies below the root can still reach many tests. This is conservative
runtime coupling: the selector keeps those edges rather than removing them to produce a
smaller subset, and the subset that results, possibly most of the suite, still runs as
SELECTED. Two kinds of gap keep a test in every candidate subset (its location and reason
stay in the JSON `diagnostics` field and stderr):

- An unresolved load (`import_module` of a computed name, a file loader, `python -c`
  source that cannot be read) may run any code, so it always counts.
- A read whose path cannot be anchored to the checkout or to an outside root
  (`path.read_text()` on a parameter) can only matter to a data file: Python sources reach
  tests through the import graph, and the tests that read source text are the pinned
  tree-scan family. It counts only when the change includes a data input whose readers are
  not already represented by its path class or declaration. Python, Markdown, meizel
  manifests, `path_scopes.toml`, `ambient_roots.toml`, `scripts/lint/baseline/`,
  `scripts/structure/baseline/` and `scripts/structure/skip_sites.json` do not activate it.
  Neither do TREE_SCAN_ONLY or FRONTEND inputs: their recognized readers and owning CI
  jobs already cover them. Markdown is prose, and the tests that read skill or OKF documents
  are pinned by name instead: a changed `SKILL.md` or `*.okf.md` selects every collectable test
  whose code holds that name as part of a string constant (docstrings and comments read
  nothing). A mixed diff containing another data input still includes tests reaching
  unanchored reads. Recognized resource edges are retained for every class; this rule only
  controls unrelated unanchored reads. The post-merge full runs catch anything else, after
  it reaches `main`.

A gap is not one when the file declares it. The shared fact collection reads
`meizel.policy.toml`: a `[loads]` entry becomes dependency edges to its directories, and an
`[external]` entry (honored for sources only; a listed test may load a repository file by
path) says the loaded content is not part of the repository. An existence probe
(`importlib.util.find_spec`) loads nothing and is never a gap. Explicit edges do not
certify that an unrelated opaque input is resolved. Syntax errors, malformed
declarations and unexpected analysis errors fail the selector job and the required
backend check.

Documentation is also an input when the shared resource graph proves a possible reader.
The workflow's classify job and the selector query the same head/base evidence before
skipping a documentation-only diff. Inputs with known readers request backend CI;
unrelated unknowns alone do not turn an unread documentation change into a runtime edge.
Classification analysis errors fail the required backend check as well. Each classify
invocation keeps its base and changed-path evidence in a private temporary directory,
removed on success or failure; concurrent invocations cannot replace each other's inputs.

The existing e2e-env-guard job is outside this selection path. It continues to
run its complete tests/e2e/ package plus tests/harness/test_home_isolation.py in one
serial process whenever either side changes; no selector output feeds it. The
e2e shards, hosted e2e and e2e-env-guard jobs run for every diff that is not
documentation-only (the `classify` job marks any non-documentation path outside
ui/web/ as backend), so a tests/e2e/ change never depends on the backend subset
to be exercised.

## Path classes

`classify_path` gives every changed path exactly one class; `select_tests` and
the tracked-tree completeness test share it, so the rules exist once. The first
matching row decides.

| Order | Path | Class | Contribution |
| --- | --- | --- | --- |
| 1 | A documentation path (see below) | DOCUMENTATION | runtime readers from either tree |
| 2 | Root `conftest.py` | GLOBAL | full suite |
| 3 | Any other `conftest.py` (also when deleted) | CONFTEST | every collectable test below its directory plus runtime consumers |
| 4 | Absent from the head tree | DELETED | base-tree impact; FULL (impact unknown) with a diagnostic when base facts are unavailable |
| 5 | A collectable test file | TEST | itself and its transitive test consumers |
| 6a | A root meizel manifest (`meizel.toml`, `meizel.policy.toml`) | MANIFEST | runtime readers, meizel's own tests, every test naming the file and the tests of each source module that names it (below) |
| 6b | A global path (below) | GLOBAL | full suite (impact unknown: it applies to every test) |
| 7a | `.github/`, `.agents/`, `.meiva/` | TREE_SCAN_ONLY | tree-scan tests plus every collectable test whose source names that top-level directory (below) |
| 7b | `demos/`, `tests/e2e/`, `.pre-commit-config.yaml`, `.gitignore`, `.gitattributes`, `.gitleaks.toml`, `LICENSE`, `NOTICE`, and Markdown under a global prefix (below) | TREE_SCAN_ONLY | tree-scan tests plus recognized runtime consumers |
| 8 | A non-Python file under `ui/` | FRONTEND | recognized backend resource consumers, when backend selection runs; the frontend job also owns it |
| 9 | Any other file under agent/, meiva/, meiva_builtins/, base/, cli/, gateway/, ops/, schedules/, scripts/, services/, tests/ or ui/ | PACKAGE | transitive runtime consumers plus the owning package's tests |
| 10 | Anything else | UNMAPPED | full suite (impact unknown: no rule owns the path) |

The root meizel manifests are declarations: `[roles]` names the top-level packages the
selector classifies by, and `[uses]`, `[debt.*]` and the policy feed the meizel checks, which
the structure job runs over the whole tree on every CI run. No runtime behavior reads them,
so a change selects the tests that can observe them: every collectable test under `meizel/`,
every test whose code holds the file name as a string constant (outside docstrings), and the
runtime and package tests of each source module that does. The readers are derived by that
scan, never listed. The test hosts come from `pyproject.toml`, which stays global.

Global paths apply to every test: `pyproject.toml`, `uv.lock`, `.python-version`,
`.env.example`, the root `conftest.py`, the modules its `pytest_plugins` lists
(plus the `__init__.py` of every package on their import path), and everything
under tests/fixtures/, db/, migrations/, deploy/ and commands/. This list and
the plugin set are the single "global path" concept in the selector. Two cases that
looked global are not, because their impact is computable rather than unknown:

- Markdown under one of those prefixes (for example `deploy/lgtm/README.md`) is prose,
  not an input a run loads. It is TREE_SCAN_ONLY: the tree-scan tests plus the tests that
  read that file by a recognized path run, the way a documentation change selects its
  readers. Every other file under the prefixes, including `db/schema.sql`, stays global.
- `pyproject.toml` is global only when the tables that changed can reach a test run. The
  selector parses the committed base and the head text and compares them table by table (the
  top-level tables, and `tool.<name>` for each tool). When every changed table is `tool.pyright`,
  `tool.basedpyright` or `tool.ruff` (a checker configuration no test run loads, which is where
  each new `tests/` directory registers its execution environment) or no table changed at all,
  the file is TREE_SCAN_ONLY too: the tests that read `pyproject.toml` by path run. Any other
  table (`project`, `dependency-groups`, `build-system`, `tool.uv`, `tool.pytest`, the test
  hosts, coverage, an unlisted tool) keeps it global, and so does a missing base: `--base-ref`
  absent, or `pyproject.toml` new in the change. The listed set is closed.

Tests read `.github/`, `.agents/` and `.meiva/` by path. Recognized
resource facts reach their transitive consumers. A change there selects, besides the tree-scan tests, every
collectable test whose source contains the directory as a string literal: the
unquoted `.github/` (not preceded by a word character or a dot, so the module
name `base.agents` does not match `.agents`) or the quoted `".github"` or
`'.github'`. Test sources are scanned at selection time; no map file is stored.

The owning package of a path is found by walking up from its directory: the
nearest `tests` directory that holds at least one collectable test owns it, and
every collectable test below that directory is the package's test set. The
directory itself counts when the path is already inside a `tests` directory,
otherwise a `tests` directory beside an ancestor counts. A path with no nearer
owner falls back to the top-level tests/ directory. A file's transitive runtime consumers are
added to its package tests.

### Interface early cutoff for strict modules (`meizel tests` only)

`python -m meizel tests --changed <paths> --base-ref <ref>` still offers this narrowing for a
local run. CI does not use it: `scripts/ci/test_selector.py` stopped applying the cutoff in
#8250, after #8154 showed the inference is wrong - a test asserts behavior, not a signature -
and `meizel impacted`, which CI uses to scope basedpyright, never applied it (it is a plain
reverse import closure).

In that CLI path, a changed `PACKAGE` Python source file cuts off transitive cross-package
consumer propagation and selects only its owning package's internal tests (plus the pinned
tree-scan gates) when all of these hold in both base and head:

- the file has effective `purity = "strict"` in `meizel.toml`, carries no recorded
  `[debt.*]` entry in any ancestor `meizel.toml`, and its directory's manifest boundary
  declarations (`exports`, `loads`, `external`, `state`, `roots`) are unchanged;
- its public AST interface fingerprint (`meizel.test_graph.cutoff.module_interface_unchanged`)
  is unchanged: every public function, class, public or dunder method, public field,
  module-level public constant or type alias, `__all__` / `__all_for_meiva__`, and
  imported symbol referenced by a public signature or re-export is identical, with no
  top-level control flow;
- its owning package has a package-local `<pkg>/**/tests/` directory (not the top-level
  `tests/` fallback) and at least one collectable test in that package reaches the file
  in the runtime import graph.

The fingerprint reads names, signatures, annotations as written, defaults, decorators, bases,
class-level fields and re-exports. It does not read function or method bodies, so it cannot see
an attribute type assigned inside one (`self.items: list[str]` in `__init__`), or the bound of a
private `TypeVar` - or a private alias - that a public annotation names. Both change what a type
checker sees while the fingerprint reports "unchanged", so a selection made through this cutoff
drops the consumer that would have caught them. Treat it as a local shortcut, not as a claim
about what a diff can affect.

Any change to a public parameter name (`backoff_ms` to `backoff_s`), annotation,
default value, return type, decorator, class field, public constant or re-export
changes the fingerprint and runs the full transitive consumer closure.

`--base-ref` also gives selection the base tree for deleted-path recovery: without it, a deleted
path has an unknown impact and reports FULL (`incomplete-impact`). Base facts are read from a
temporary Git archive, never from an unrelated working checkout.

## Decision rules

The first matching rule decides the outcome. FULL keeps the full backend suite;
SKIP keeps the existing documentation-only behavior (no backend suite); only
SELECTED replaces the backend pytest fan-out, and only in enforce mode.

FULL has two kinds of cause and no third. Rules 1, 2 and 4 keep everything by
construction: a non-PR event, a batch whose member list this run cannot read,
and a batch that changes both sides of the frontend/backend contract. Rules 5
to 7 are impact unknown: the selector cannot say which tests a change affects,
so it does not guess. A large computed impact is not a cause: no rule compares
the subset with the full suite.

Rules 2 and 4 are defensive, not live. They fire only for a head branch named
`trunk-merge/*` or `trunk-temp/*` — the shape the retired Trunk queue used. No
workflow produces one now, so on every current pull request they are dead
weight the selector keeps on purpose: if such a branch ever appears again, a
batch whose member list cannot be read, or that spans both sides of the
frontend/backend contract, must keep the full suite rather than select from a
list it cannot check.

That guard is keyed to the branch shape, not to queues in general, and the
difference is worth stating: a future queue that runs as another event type
never reaches this selector at all — GitHub's own merge queue arrives as
`merge_group`, the step is gated to `pull_request`, and a run with no selection
keeps the full eight-shard fan-out, which is rule 1's decision made one layer
up. A queue that instead reused `pull_request` under a new branch prefix would
select like an ordinary pull request until that prefix joined `_QUEUE_PREFIXES`;
adding it there is the whole fix, and the tests pin the behavior behind it.

| Order | Changed-path or event condition | Result |
| --- | --- | --- |
| 1 | Not a pull_request | FULL (non-pr-event) |
| 2 | A `trunk-merge/*` batch branch whose member list cannot be read out of its body | FULL (queue-members-unknown) |
| 3 | Every path is documentation and has no known runtime reader in either tree | SKIP |
| 4 | A batch branch that changes both a frontend path and a backend one | FULL (cross-domain-batch; the payload names one path from each side) |
| 5 | A path is GLOBAL | FULL (impact unknown; `global-path:<first path>`, the payload lists every global path) |
| 6 | A path is UNMAPPED | FULL (impact unknown; unmapped, the payload lists the paths) |
| 7 | A deleted path without base facts | FULL (impact unknown; incomplete-impact with diagnostic locations and reasons) |
| 8 | Otherwise, union the contribution of every path, the tree-scan tests and every test reaching an opaque input in either tree | SELECTED (owner-tests) |

Both rules apply to such a branch only, and they are the whole difference
between a batch and a pull request. A batch's synthetic pull request listed the
pull requests it tested under `## Pull Requests Being Tested`, and those numbers
reached the run in the event payload — no token, no network. The batch's
`changed.txt` is already the union of its members, so the ordinary rules measure
it once; what a subset cannot vouch for is the completeness of that union, and a
batch whose member list is missing keeps the full suite rather than selecting
from a list it cannot check. A batch whose body names members but whose changed list is empty keeps
the full suite as well (`queue-members-no-changes`): the two contradict each other, and the cheap
forks are the only ones this selector can take on its own.

For such a branch, the **completeness** of that changed list is the pipeline's guarantee, not the
selector's: the classify job diffs the batch tree against its merge base, so the list is every path the batch
changes. The selector never re-derives a member's own diff — that would mean one GitHub read and one
more permission per member, which is what #5756 deliberately avoided — so a body that names members
whose changes are absent from the list is a shape the pipeline cannot produce, and the selector does
not pretend to detect it.

A batch branch that spans the frontend and the backend
keeps the full suite too: the two sides meet in a contract — a route, a payload,
a generated type — that no owner rule crosses, so a subset built from each side
alone can miss the test that reads both.

A change that no test consumes still selects the tree-scan tests, so rule 8
never yields an empty candidate in the repository (a candidate that held no
test at all would be a checkout without a single tree-scan test, which
tests/scripts/test_test_selector_contract.py rules out for the repository).

Rule 6 is a runtime safety net. scripts/tests/test_test_selector_owner_rules.py
classifies every tracked path and fails when any is UNMAPPED, so a tracked path
never reaches rule 6; a new top-level directory or root file needs a class in
the selector before it can merge.

Tree-scan tests join every candidate subset, including one whose changed paths
reach no test: every `test_lint_*.py` under `tests/` (any depth, non-e2e) and the repo-level
CI/governance checks pinned in `scripts/ci/test_selector.py`
(`TREE_SCAN_TESTS`). The owner rules cannot reach a repo-wide scan
test from a changed source file, and a green subset must not miss a
tree-wide gate (task #4183: PR #3020's subset passed while the full
population was red on tests/contracts/test_lint_event_kinds.py). Name a new scan test
`test_lint_*.py` to join automatically, or extend `TREE_SCAN_TESTS`;
tests/scripts/test_test_selector_contract.py guards completeness and staleness.

The harness is the Python every pytest process loads before any test: the root `conftest.py`,
the modules it lists in `pytest_plugins` (with their package initializers) and the
`tests/` modules they import in turn. A change to the harness is global (FULL), and the selector
does not also charge its production imports to each test: counting them made 327 inputs
(`base.log`, `base.config`, the telemetry and supervision modules the fixtures import lazily)
reach all of the suite, so any change to one of them selected everything. What the
selector gives up is the signal that a production change broke the harness's own imports, so
`scripts/ci/tests/test_harness_imports.py` restores it directly: `harness_imports.py` imports
every first-party module and name the harness imports, at any depth (static imports, so a broken
one fails collection), and the test asserts that list equals what the harness really imports. It
is a tree-scan pin, and it also runs in the static lane on every backend change. Behavior of the
harness's production dependencies is covered by the post-merge full runs, as for every subset.

The documentation predicate reuses base.deploy.git.repo_change.is_doc_path, the same
owner as CI's frontend/backend classifier. It recognizes the existing project doc
axes and top-level Markdown, plus `.meiva.okf.md` files in a component's `docs/`
layer, including under scripts/ and ui/web/. Test data in a `tests/` directory
does not qualify as a component document. Other nested Markdown, including
AGENTS.md and SKILL.md, retains its code-directory classification.

Files under schedules/ and any `tests/` directory (the top-level one or a package's
own `<pkg>/**/tests/`) are never documentation: they are PACKAGE paths. The classifier's
existing schedule policy is unchanged. The classify job decides the two sides from the diff
alone and only passes the head ref through, so no branch shape can force a side; a
non-pull-request event enables both sides before the selector runs. The independent
documentation, language, content-manifest and security gates run on documentation
PRs too.

Tests live in the top-level `tests/` or beside the code they prove in
`<pkg>/**/tests/`. The host directories come from
`tool.pytest.ini_options.testpaths` in `pyproject.toml`, the same owner used by
pytest collection, the pyright test-environment generator and the fixture-scope lint.
Supported entries are the literal `tests` and `<host>/**/tests`; missing configuration
or unsupported patterns fail rather than silently narrowing a tool's scope. A unit test sits in the `tests/` directory of the package it tests; an
integration test across packages sits in the lowest package that may legally import
everything it uses; end-to-end tests and contract tests that read repository artifacts
stay in the top-level `tests/` ([testing guide](../../../tests/README.md#where-to-put-tests)).
The selector treats both alike: a test-only edit under `base/x/tests/` is a
TEST path that runs itself, a helper or data file there belongs to that
package's tests, and a module that merely carries a `test_` prefix outside a
`tests/` directory (`scripts/ci/test_selector.py`) is not a test.

## Runtime impact facts and limits

The dependency graph AST-parses Python files under the shared code roots and `tests`,
including non-collectable helpers and conftests. Each collectable non-e2e backend test
is a graph root. Module edges also include concrete package initializers that Python
executes during import. Dependency cycles terminate through a visited set. A helper
or imported test affects its transitive test consumers even when those consumers live
outside its nearest ownership bucket.

The shared fact collector keeps line numbers, edge kinds and unresolved expressions.
Rooted resource paths are evidence of possible use, not proof of a read or runtime
coverage; directory paths conservatively affect changed descendants. The shared
collector can narrow a complete local immediate-entry discovery to one literal
child filename. Its member domain matches additions, deletions and renames in
both base and head; unrelated nested files do not match. Escaping, rebound,
replaced, aliased or unsupported discovery keeps the coarse edge. Opaque root
alternatives retain unknown evidence, and helper/default discovery such as the
dashboard metrics reader remains conservative. No test-directory exclusion or
runtime discovery rewrite participates in this narrowing.
It supports bounded static evidence, not arbitrary execution or reflection. Known
external modules have no repository input to select. Unresolved dynamic inputs remain
explicit unknown evidence. Fixtures registered through `path_scopes.toml` create real
edges, but an opaque call elsewhere in a plugin remains unknown unless its finite input
domain is proved; its consumers then join every subset. When an opaque input sits in a
module that many tests reach, the subset is large on the current repository; it is not
evidence that the runtime dependency closure is small or complete.

The graph is rebuilt for every run and no coverage-derived map is committed. Package
ownership and tree-scan rules remain conservative additions. The full-suite runs outside PR
selection — the post-merge Release Train, the nightly verdict and every non-PR event — remain
protected, including when ordinary PR selection is incomplete.
A selector `tests` list contains candidate files, not an executed test count: the
selected native lane omits the independently executed static lane and flaky tests,
while the serial lane owns flaky execution. JUnit records remain the execution evidence.

## Subset shards

A SELECTED subset runs on as many `backend selected subset` shards as its share of the suite
needs at the size of one full shard: `ceil(est_seconds / full_est_seconds * 8)`, clamped to
1..8 and to the number of selected files, with the share taken from file counts when no timings
exist (`subset_shard_count`, with the width 8 owned by `FULL_FANOUT_SHARDS`; a contract test
ties it to the full fan-out's matrix and `--count`). A subset that is the whole suite is eight
shards and as fast as the fan-out; a few files are one short job.

`classify change` publishes `shards` and `groups` (the JSON list `[1, ..., shards]` that is the
matrix) as job outputs, and the selected file list as the `selected-tests` artifact, because the
whole suite as one environment string already reaches Linux's 128 KB limit. Each shard
downloads the list and splits it with the full fan-out's own planner
(`scripts.ci.pull_requests.plan_file_shards --files-from`): a duration-balanced whole-file plan
over the selected files only, which each shard re-collects and checks before it executes its
group. Every shard has the full shards' 20-minute limit. The verdict is the matrix job's
result, which is also what the shadow report reads.

## Duration estimates

The timing input is the repository-root `.test_durations` file. It is not tracked: the daily
isolated shard measurement ([refresh-test-durations.yml](../../../.github/workflows/refresh-test-durations.yml))
saves it as an Actions cache and `classify change` restores it. The estimates only report:
`est_seconds` and `full_est_seconds` appear in the JSON payload and the `test-selection:` log
line, and no decision reads them. A run without the cache estimates zero for both and selects
exactly the same subset.
It maps pytest node IDs to seconds. Refreshes retain every measured node,
including fast tests and durations that round to zero; only unmeasured nodes
use pytest-split's average fallback. The selector sums entries whose node ID
starts with each selected test file plus ::; a file with no timing entry costs
the average present backend timing entry. A recorded zero remains zero. The
same model estimates the complete collectable backend universe.

The estimates size the shards of a subset (above) and nothing else: no decision reads them.

`scripts.ci.test_selector.load_durations` and `estimate_seconds` own this model
for the selector and its reporting consumers. The read-only
`python -m scripts.audit.impact_census --json` command reports source reach and unbounded
input consumers from the same impact graph, using these functions for estimated
minutes. Census estimates describe static coupling; they are not measured CI
execution times or proof that a selected subset is faster.

## Skipping e2e for changes outside the e2e inputs

e2e is the integration net for product behavior, and an incremental pull request does not run
it: the full Playwright matrix runs on every Release Train (`release-gate.yml`) and on a manual
`workflow_dispatch` run of CI. `classify change` sets `skip_e2e` from `scripts/ci/e2e_scope.py`,
and the four e2e checks (`e2e shard`, `e2e hosted`, `e2e env guard (serial)`,
`e2e (Playwright happy path)`) are skipped, which counts as satisfied for the required checks,
only when all of these hold:

- the event is a pull request;
- test selection is enforced and its decision is non-empty - a frontend-only pull request passes
  `FRONTEND_ONLY`, and an empty decision, as after a selector failure, keeps e2e;
- at least one path changed;
- no changed path is an e2e input: `tests/e2e/`, `.github/ci-image/`,
  `.github/workflows/ci.yml`, `scripts/ci/e2e_scope.py`,
  `tests/harness/test_env_guard_canary.py` or `tests/harness/test_home_isolation.py`;
- none of the changed paths is reached by the e2e suite's own import closure
  (`e2e_reached_unit_tests`); that walk fails closed, so a file or an import form it cannot
  understand keeps e2e.

What the change is does not enter the decision. A diff that only edits unit tests, a
`meizel.toml`, prose under `docs/`, and a product module whose function bodies changed all skip
e2e alike, because none of them reaches an e2e job. The rule does not ask "could e2e observe
this change": the net for a pull request's behavior is the scheduled full matrix, not a
per-diff judgment. The strict-module interface fingerprint that used to answer that question
here was deleted with the rest of that path in #6052; test selection had already stopped
consulting it in #8250, and the cutoff argument had stopped reaching this rule in #8091.

The head ref is never read: a pull request on any branch is judged by its diff alone.
`scripts/ci/tests/test_e2e_scope.py` pins the rule, the workflow wiring, the e2e import
reachability walk, and the root manifest isolation of `tooling` packages.

## Selection modes and artifacts

`TEST_SELECTION_MODE` in [ci.yml](../../../.github/workflows/ci.yml) is the single
switch. The `classify change` job republishes its value as a job output — job-level
routing (`if`, `continue-on-error`) cannot read `env`, only `needs` — and every
routing expression keys off it.

**enforce** (the default since 2026-09-11):

- A SELECTED real PR runs `backend-selected` (a matrix of 1 to 8 shards) instead of the
  shard fan-out: the selected subset is the backend pytest gate, and the aggregator (the
  branch-protection check `backend (pytest + pyright)`) requires every shard.
- `backend-selected` uses the native pytest result and the shared JUnit
  validation gate, like the shards. Test failures remain red; no external
  quarantine service or secret determines the verdict.
- The aggregator still requires static / structure / serial flaky / pgvector
  smoke / helper signing exactly as before.
- No coverage artifacts are produced on that path: the 85% full-tree coverage
  gate stays on the full fan-out — every non-SELECTED run produces one.
- A selector or setup failure leaves routing outputs empty, which retains the
  full-suite path, and also fails the required backend aggregator. A parser
  failure cannot become a successful FULL decision.

**shadow** (the revert switch):

- No PR gates on the subset. The full fan-out gates exactly as before the
  enforcement switch; the subset runs informationally; the
  `test-selection-shadow-report` job records the divergence comparison — FALSE
  GREEN only when a passing subset meets a failing full non-flaky pytest
  population.

## Maintenance and the revert switch

- Review any false green immediately and close a real rule gap; no false
  green is accepted as a known exception.
- Keep selector unit tests focused on observable decisions, AST resolution,
  current-test filtering, duration estimates, batch branch shapes, per-class owner
  rules, the tracked-tree completeness guard, and deterministic JSON.
- The daily workflow keeps the timings current; nothing to refresh by hand after
  material suite changes.
- Revert: set `TEST_SELECTION_MODE: "shadow"` in ci.yml (one line) and update
  the enforce-default assertion in tests/scripts/test_ci_test_selection.py.
  The wiring contracts deliberately turn red when the switch moves — that
  tripwire is what keeps a revert from silently losing a gate.

## History: the shadow window and the enforcement decision

Shadow mode ran from 2026-09-03 (its introduction) through 2026-09-10. Its
comparison artifacts recorded 978 distinct CI runs: 716 on pull requests (464
distinct PRs) and 262 push runs, where the selector does not run and the
report is a no-op. Decisions: FULL 680 (forced roots and unmapped paths
dominated), SELECTED 31 runs / 25 PRs, empty 267 (262 push runs plus a handful
of concurrency-cancelled runs). One run recorded FALSE GREEN (PR #1842,
2026-09-06): triage attributed it to a time-dependent assertion in
a daemon test in `tests/components/services/` — unrelated to that PR's diff, and
fixed the same morning by #1840 (merged five minutes after this run's decision
was recorded). Its decision payload had no changed blind file, so no static-map
gap was involved. No other false green was observed, and no informational
false-negative (subset red while the full population passed) either.

2026-09-10 23:26 user ruling: switch to enforcement directly; the earlier
staged criteria (a 100+ PR window with a monthly re-measure, every false green
fixed as a blind-map gap) are not a precondition for this switch. They stay
recorded here as history, and the false-green rate remains the review signal
while enforce is live.

The synthetic checkouts in `scripts/tests/test_test_selector*.py` need no timing file
for owner selection: durations only feed the estimates, and
`scripts/tests/test_test_selector.py` pins that a subset above 80% of the full estimate, or over
half of a suite larger than the subset job, is still SELECTED.

2026-10-24 user ruling: the duration guard is deleted (rule 8, FULL when the candidate
exceeded 80% of the full estimate or, on a large suite, half of it, and rule 8a, FULL
when no timings were restored), together with the empty-candidate FULL (rule 7). How
large an impact is too large has no definition, and the Trunk queue then ran the full suite
before anything reached main (it was retired on 2026-11-01; the post-merge runs carry that
argument now). In the ten pull requests of the day before, nine ran the
full fan-out: six on a global path, which this ruling keeps (their impact is unknown, not
large), and three whose candidate was the whole suite, which now run as SELECTED.

The same day, the three consequences of running a whole-suite subset were settled with it. The
subset is sharded by its estimated time instead of running as one job under one timeout. Its
list travels as an artifact instead of an environment string. And two global-path rules that
mistook known-confined changes for unknown ones were narrowed (Markdown under a global prefix,
and `pyproject.toml` tables that only configure a checker); the rest of the global paths are
unchanged.

Also 2026-10-24: the selector stopped counting the root conftest's import closure against each
test (it already treated the root conftest, its plugins and `tests/fixtures/` as global), and
the harness import contract above took over the signal that a production change broke the
harness's imports.

## What verifies a merge

Four facts about the current pipeline, kept apart because they answer different questions. Only
the first is a check on a merged tree.

**A pull request's checks run on a merged tree — the one that existed when the run started.**
`ci.yml` is triggered by `pull_request`, so `github.sha` is `refs/pull/N/merge`: the pull request
head merged into the base of that moment. The suite does see the combination, not the head alone.

**The base can move afterwards.** Branch protection sets `strict = false` — what is required is
declared in `scripts/audit/analysis/protection.py`, and the weekly audit compares that declaration
with the live setting — so a pull request whose base advanced after its run is not re-tested
against the new base. Two pull requests that are each green against their own base can be red
together. This is a known and accepted trade, not an oversight: the merge rule does not force a
rebase onto the latest base, so the recorded verification is the one that actually ran.

**Nothing verifies the merge order.** There is no queue, no serialized integration branch and no
combined-tree job; the Trunk merge queue that provided one was retired on 2026-11-01. A
merge-order problem has to survive the post-merge runs below.

**No test suite runs on a push to `main`.** `ci.yml` has exactly two triggers, `pull_request` and
`workflow_dispatch`; merging to `main` starts no CI run of its own. (Two narrow push workflows exist
— `cache-warm.yml` and `ci-app.yml` — and neither runs tests.)

### Where the full suite runs

Two scheduled workflows run the whole population on `main`, each for a different question:

| Where | When | What it answers |
|---|---|---|
| `.github/workflows/release-gate.yml` | every six hours (`17 */6 * * *`), and on demand before a wave; skipped when `channel/latest` already points at `main` HEAD | may this tree be called `vX.Y.Z` — and, as a side effect, was `main` green |
| `.github/workflows/refresh-test-durations.yml` (nightly, main) | daily (`30 19 * * *`) | was main green last night — the `verdict` job reads junit only, so a retried measurement cannot wash a first-attempt failure green |

The nightly run is the honest answer to "is main broken", and a red night blocks the release
gate. It does **not** stop main from accepting merges: a nightly flake must not become a
cluster-wide stop. The release gate is where a red night becomes blocking.

Put together, the coverage is: **one verification per pull request against the base it started
from**, and then a full-matrix run on `main` roughly every six hours plus a verdict every night. A
gap in the PR-side subset, or a combination that only breaks when two green pull requests land
together, reaches `main` and is found hours later by one of those two runs — found, not prevented.
