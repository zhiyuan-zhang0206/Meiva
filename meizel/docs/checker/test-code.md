# meizel/ — Test code

A test that drives its own mock reaches no collaborator of the code under test: the module belongs to
the suite, not to what ships. So a `first-party-patch` whose target is such a module is not recorded,
and neither is a `vi.mock` of it. Two conditions hold, and both are facts of the tree rather than
entries in a list:

- The target module is test code by the test-file convention (`is_test_file`), which reads each
  language the way that language names its tests. For Python: `conftest.py`, `test_*.py`,
  `*_test.py`. For TypeScript: a `*.test.*` or `*.spec.*` file, a file under a `tests/` or
  `test-support/` directory, or the frontend's shared mock named `*-test-mock.tsx`. The directory rule
  is TypeScript's alone - `tests/fixtures/guards.py` is not test code, so a patch of it is recorded.
  The mock suffix is the frontend's own convention, the one `local/no-test-only-import` and
  [test-only imports](../../../ui/web/src/docs/frontend-components/test-only-imports.md) already
  state; a bare `-mock` was wider than either and is not test code.
- Nothing outside that convention imports it. This is the condition that matters: the day any file
  outside the convention imports the mock, the module is part of what ships and every patch of it is
  recorded again. A file production reaches is never test support, however it is named.

The checker reads the run's own import graph, so the exemption is exactly as wide as the analysis:
every Python file, and every TypeScript file under a `[typescript]` `scopes` or `import-sources`
path. A `--changed` run knows only the files it scanned, so an import it did not scan cannot clear
the exemption; the full `check` sees the whole graph. It is a rule of the check, not of the tree's
inventory: `debt sites` still lists every patch site in the source, a patch of test code included,
because it reports what the tree holds rather than what the ledger counts.

Nothing about this is declared in a manifest or a policy table: a listed file that production
imports would be stale the moment it is imported, and the import graph already says which modules
ships carry.

## What the rule withdraws today

| module | records | test files that write it | why it is a double |
|---|---:|---|---|
| `ui/web/src/components/agent-sidebar/sidebar-test-mock.tsx` | 141 | `agent-sidebar.test.tsx` (34), `sidebar-status-cards.test.tsx` (56), `sidebar-tree-and-search.test.tsx` (51) | the rail's scenario object: the props the page hands it, and the settings, stats and plugin declarations it reads |
| `ui/web/src/app/page-test-mock.tsx` | 111 | `page.test.tsx` (61), `page-dispatch.test.tsx` (50) | the home page's `hooksState` and its provider wrapper |
| `ui/web/src/components/inspector/inspector-test-mock.tsx` | 4 | `inspector-panel-chrome.test.tsx` (3), `inspector-panel-state.test.tsx` (1) | the inspector's `inspectorMockState` |

256 records, every one of them a write into a state object the double exports: no `vi.mock`, and no patch
of a module the suite renders. `python -m meizel debt sites --code first-party-patch --json` lists every
site (259 today, 238 at `1d6710493`); 256 name one of these modules, 2 sit in `[approved-patches.files]`,
and 1 is recorded. A repayment lowers these numbers and a new site raises them, and
`meizel/tests/languages/test_test_code_accounting.py` fails until this table and the tree agree; the same
test injects a production import of each double into the run's fact graph and asserts every record comes
back, which is the condition above under test rather than in prose.

A record is one write into the double, so each number is a per-file upper bound and a legitimate refactor
moves it in either direction: task #6548's hover card reads the agent's model and reasoning effort off the
roster, so the rail's scenario agent gained those two fields — `sidebar-test-mock.tsx` 138 → 140; #8080 retired `display.composer_context_meter`, which took the two
assignments that set it, and merged two setup lines — `page-test-mock.tsx` 95 → 91; task #6540's two Status-card tests write the rail's scenario object, so
`sidebar-status-cards.test.tsx` moved 46 → 48; the Inbox removal dropped the three roster counters from the rail scenario agent and deleted the awaiting-reply tests — `sidebar-test-mock.tsx` 140 → 135. Every change to this
table therefore says which sites moved and why; the test can report that a number moved, never why.

The name is the whole review surface of this half, and that is why the suffix is `-test-mock` and not
`-mock`. While the rule
read a bare `-mock`, a new file whose name happened to end that way was test code the moment it appeared:
a `probe-state-mock.ts` written for that measurement, with one test writing its state, produced no record,
and importing it from a production file brought the record back. `page-mock.tsx` and
`page-fixtures-mock.tsx` were exempt under that reading and are recorded now; neither holds a record
today, which is why narrowing the suffix moved no count.

Parent: [patching](patching/patching.md).

The composer question-queue regression adds three assignments to the existing
home-page double: selected agent, agent roster and timeline items. Those inputs
exercise per-agent question filtering and keep pending forms outside the
visible timeline. Its documented records therefore move from 91 to 94; the
production-import control and conditional test-only exemption are unchanged.

The seen-position regression adds the same three assignments (selected agent, agent roster and
timeline items) to the home-page double, to drive which delivery the page asks the timeline to
reveal. Its documented records move from 94 to 97; the production-import control and
conditional test-only exemption are unchanged.

The home reader-loading regression adds four writes in `page-dispatch.test.tsx`
to configure the real timeline consumer and its home-page scenario. The home
double therefore moves from 97 to 101 records; the exemption rule is unchanged.

The structured quota reset regressions add six writes in
`sidebar-status-cards.test.tsx`: each of the two scenarios sets the agent roster,
plugin declaration and stats response on the existing sidebar double. They
exercise countdown transitions and retained-error observation age through the
real sidebar consumer and its clock/provider ports. The status-card file moves
from 50 to 56 sites, the sidebar double from 135 to 141, and the three doubles
from 240 to 246 records (249 raw first-party patch sites). The conditional
test-only exemption and the production-import control are unchanged.


The Home bookmark return regression adds six writes in `page-dispatch.test.tsx`:
three selections drive A → B → A, while the roster, page-entry identity and
settings establish the real Home consumer. The dispatch file moves from 40 to
46 sites, the Home double from 101 to 107, and the three documented doubles from
246 to 252 records (255 raw first-party patch sites). These are exact consumer
counts, with the historical measurements above retained. The conditional
exemption and production-import control are unchanged. Changes that add writes
to these doubles must scan the raw sites and run the exact-accounting test;
a green dependency graph does not establish that these published counts agree.

The per-selection authority regression adds four writes in `page-dispatch.test.tsx`:
selected agent, roster, page-entry identity and settings configure cached Home
entry with unchanged, replaced, empty, failed/retried and unsaved-position reads.
The dispatch file moves from 46 to 50 sites, the Home double from 107 to 111,
and the three documented doubles from 252 to 256 records (259 raw first-party
patch sites). These source scan counts preserve the historical measurements
above; the exemption rule and production-import control are unchanged.
