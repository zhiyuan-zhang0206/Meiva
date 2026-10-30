# Code placement, purity and dependency flow

Where a new or refactored symbol belongs, which `meizel` purity tier owns it, and how dependencies flow from composition roots through effect shells into pure logic. Read before splitting a module, moving a type across packages, or repaying `meizel` locality and purity debt.

For top-level package ordering (`base < meiva < agent < gateway < cli`), see [import layering](../import-layering.md). For the full dependency-injection rules and port/fake catalog, see [tests do not patch; production takes what a test needs to control](../python-conventions.md#tests-do-not-patch-production-takes-what-a-test-needs-to-control).

## The three roles inside a component

Every component separates **pure decisions**, **effect execution**, and **process wiring** into distinct modules so `meizel` can enforce their boundaries statically ([checker](../../../meizel/docs/checker/checker.meiva.okf.md)):

| Role | `meizel.toml` declaration | What belongs here | What is forbidden here |
|---|---|---|---|
| **Pure core** | `purity = "strict"` (directory or per-file under `[purity]`) | Frozen dataclasses, Pydantic `BaseModel`s, `StrEnum`s, narrow `Protocol`s, parsers, state transitions, and pure functions that take values and return a decision or plan | Importing a library on the `impure-imports` list of `meizel.policy.toml` (`psycopg`, `httpx`, `redis`, `subprocess`, `socket`, `shutil`, `tempfile` and the rest of that list), reading ambient process state (`settings`, `os.environ`, `time`, `uuid4`, `Path.home`), or importing any non-`strict` first-party module (`impure-edge`) |
| **Effect shell** | `"strict-to-be"` while a directory is migrating; its end state is an explicit `{ mode = "none", why = "..." }`, only once the pure decisions are extracted and an independent review has confirmed it (an unreviewed downgrade is an evasion) | Loops, handlers, and I/O adapters that execute a plan produced by a `"strict"` module using explicitly passed handles (`Database`, `SyncPool`, `AsyncPool`, `httpx` client, `run_bounded`) | Reading ambient process state (`ambient-read`), constructing its own default clients (`client: Client = Client()`), or burying testable branching inside I/O |
| **Composition root** | Listed under `[roots]` with `kinds = [...]` and `why = "..."` | Process entrypoints (`daemon.py`, `app.py`), CLI `CommandRuntime.create()`, and `wiring.py` modules that read the environment once, build frozen configs and concrete port/client handles, and pass them down | Business branching that a unit test needs to steer without starting the full process |

## Decision tree: where to place code when adding or refactoring

### 1. Shared vocabulary, wire contracts, enums, and cross-layer protocols

When two packages need the same type, constant, or `Protocol`:

- **Cross-layer contracts (`base`, `meiva`, `agent`, `gateway`, `cli`, `services`)** belong in `base/api_contracts/` (a `"strict-to-be"` directory in which most files, but not every one, are individually `"strict"`; check `[purity]` before relying on a file) or the lowest `base/<domain>/` package that both consumers may import under [import layering](../import-layering.md).
- **Never import upward** or duplicate an enum/schema across layers.
- **Keep type-owning modules `"strict"`.** If a shared `Protocol`, `StrEnum`, or dataclass currently sits in a file that also performs I/O (or whose package `__init__.py` eagerly imports storage/network modules), move the pure types into a `"strict"` module (`contracts.py`, `models.py`, `protocols.py`, or `base/api_contracts/`) and re-export them at the old path if existing callers still import from there.
- **Avoid `Any` on collaborator boundaries.** Type `Protocol` parameters, return values, and class fields with concrete `"strict"` types or `object` when the consumer only forwards the value ([`any-collaborator`](../../../meizel/docs/checker/any-collaborator.meiva.okf.md)). Note that `if TYPE_CHECKING:` is banned except for the reviewed entries in `[type-checking.files]` of `meizel.policy.toml`: import the real type from its lightweight `"strict"` owner.

### 2. Pure decision logic vs. effect execution (`impure-call` and `impure-edge`)

When `meizel` reports an `impure-call` inside a module that should be `"strict"`, or an `impure-edge` from a `"strict"` module into an effectful one:

- **Ask first whether the file is a pure core or an effect shell.**
  - If the file's job is computing a plan (e.g., retry delay, SQL query parameters, subprocess `argv`/`env`, state diff, validation), keep the file `"strict"` and lift the I/O call (`httpx`, `psycopg`, `subprocess`, `socket`) out into the calling shell or `wiring.py`. `psutil` is not on the `impure-imports` list, so `meizel` does not flag it, but reading CPU, memory or disks through it is an effect all the same: a module that does so is a shell, and the types and parsers around it stay in a `"strict"` sibling.
  - If the file contains both pure helpers/types (imported by `"strict"` consumers) and effectful execution, split it along the mechanism boundary: keep the pure types and decision functions in the `"strict"` module, and move the effectful execution into an unexported or shell sibling module declared in `[uses]`.
- **Do not coalesce re-exports in `__init__.py` just to quiet a debt line.** If a package door (`__init__.py`) is `"strict"`, every module it re-exports must also be `"strict"`.

### 3. Ambient process reads (`ambient-read`)

When code reads `settings`, `os.environ`, the clock (`time.time`, `datetime.now`), paths (`resolve_meiva_home`, `Path.home`, `Path.cwd`), platform/process facts (`sys.platform`, `os.getpid`), randomness (`uuid.uuid4`, `secrets`), or process-global DB/Redis/endpoint handles ([`ambient-read`](../../../meizel/docs/checker/ambient.meiva.okf.md)):

- **Lift the read to the composition root** (`wiring.py`, `daemon.py`, `app.py`, `CommandRuntime.create()`) that already owns `[roots]` in `meizel.toml`.
- **Pass a value or a frozen config dataclass down.** At a component boundary, pass a frozen `XxxConfig` containing only the fields that component reads; inside the component, pass plain values (`now: datetime`, `home: Path`, `timeout_s: float`).
- **Repay a read where a caller already holds the fact, not where it merely could.**
  A read leaves a file when the value was decided above it — the launcher's pid the runtime
  already carries, the instant an entry read once, the home a root resolved — or when it moves
  into a file that already declares that kind under `[roots]`. A value that *is* this process's
  own statement about itself (its pid, uid, platform, entropy, its own local wall clock), or the
  read of a door a composition root calls while every caller is a non-root module with no better
  copy, has no better provider: passing it up repeats the same system call in another file and
  leaves the same debt there, often more than once. Decide by measurement, not by reading the
  call graph: `python -m meizel debt diff --against <base>` on the branch and on the unmodified
  tree. The entry must disappear; a `+1` on the file that now holds the read is a relocation,
  and the change is reverted rather than reviewed.
- **Wrapping a read in a function does not move it.** A repository function that returns what the world decides (`meiva_home()`, `process_environment()` and the like) is listed under `[ambient-readers.<kind>]` in `meizel.policy.toml`, and a call to it is a read of that kind at the call site ([readers](../../../meizel/docs/checker/ambient-readers.meiva.okf.md)).
- **Never add an ambient fallback in the callee** (`env: Mapping[str, str] | None = None` that falls back to `os.environ`, or `now: datetime | None = None` that calls `datetime.now()`). Defaults on parameters are reserved for pure constants and tuning numbers.

### 4. Making code testable without monkeypatching (`first-party-patch`, `stdlib-patch`, `third-party-patch`)

Tests never mutate module attributes, instance dictionaries, or environment variables ([patching](../../../meizel/docs/checker/patching.meiva.okf.md)). Follow the four-level hierarchy in [data in, decisions out](../python-conventions.md#data-in-decisions-out-the-forms-in-order):

1. **Pass a value** (config field, `tmp_path`, `now`, resolved binary path).
2. **Call the pure decision function directly** and assert on the returned plan instead of spying on mock calls (`assert_called_once_with`).
3. **Run the shell against a real local resource** (throwaway Postgres/Redis fixture, `tmp_path`, local socket/listener on port `0`, small child script).
4. **Inject a narrow `Protocol` port** only when a long-lived interactive protocol is the subject or when a test must trigger a fault that no real local resource can produce:
   - Define the `Protocol` in `base/` (for cross-package ports such as `Clock`/`Timers` in `base/clock`, `UrlOpener` in `base/host/net/ports.py`, `Host` in `base/host/system/hostport.py`, `ProcessControl` in `base/native_process/process_control.py`, `SyncPool`/`AsyncPool` in `base/db/protocols.py`). Ports are defined in `base/`; a component above `base` does not define one ([python conventions](../python-conventions.md#data-in-decisions-out-the-forms-in-order)).
   - Place the reusable `FakeXxx` in the `tests/` directory of the package that defines the port, and export it in that `tests/meizel.toml`.
   - **Never introduce disguised patch seams**: no mutable class/module holder attributes (`_Holder.bound = ...`), no `object.__setattr__` or `self.__dict__[...]` mutation after construction, and no armed fault flags on production classes.

## Updating `meizel.toml` when placing or splitting files

Every directory's `meizel.toml` is a closed contract:

- **`exports = [...]`**: List only the modules or child directories that external packages import. Keep internal split modules unexported.
- **`[uses]`**: Declare the exact intra-directory import edges (`"shell.py" = ["core.py"]`). The local dependency graph must remain a DAG (`manifest-cycle` refuses cycles).
- **`[purity]` is a one-way ratchet (`"strict-to-be"` -> `"strict"`), with one reviewed exit for effect shells**:
  - Mark every pure module `"strict"` (or promote a `"strict-to-be"` module/directory to `"strict"` once all purity violations in it are resolved).
  - **Never downgrade purity without a review**: do not remove `"." = "strict-to-be"` or `"file.py" = "strict-to-be"` from `[purity]` to drop recorded `impure-call` or `impure-edge` debts back to `none`, and do not delete a `strict-to-be` directory entry. The one allowed downgrade is a file whose pure decisions have all been extracted to `strict` modules and that is only an effect shell now: it is written `"file.py" = { mode = "none", why = "..." }` (the `why` says what the shell runs and which pure module it takes its plan from), after an independent review confirms that nothing decidable is left in it. The bare `"none"` is refused (`manifest-syntax`).
  - **Never evade `impure-call` with lower-level I/O**: do not replace `Path.read_text()`, `Path.exists()`, `Path.is_file()`, `Path.is_dir()`, or `Path.stat()` with `os.open`/`os.read`/`os.stat` just to mark a filesystem-reading module `"strict"`. A module that reads the filesystem is an effect shell; keep filesystem reads in the shell and pass the read contents or stat facts into a `"strict"` pure function.
- **`[roots]` is a closed set of true composition roots**:
  - Only process entrypoints, CLI runtime factories, and `wiring.py` modules belong in `[roots]`, with the minimal `kinds = [...]` they actually read.
  - **Never add a file to `[roots]` to silence an `[debt.ambient-read]` entry.** Repay `ambient-read` debt by lifting the read in code to an existing composition root and passing the resolved value or collaborator down.

## Fast verification loop

Before moving or changing a shared symbol, trace its consumers; after editing, run the scoped checks on only the touched files:

```bash
# 1. Trace consumers before moving or changing a contract
.venv/bin/python scripts/audit/where_used.py pkg.mod:Symbol

# 2. Check locality, purity, and debt ratchet on changed files (~3s)
.venv/bin/python -m meizel check --changed path/to/file.py path/to/meizel.toml

# 3. Prune repaid debt entries from meizel.toml manifests
.venv/bin/python -m meizel debt prune

# 4. Run strict type-checking and targeted unit tests on the touched scope
.venv/bin/basedpyright path/to/file.py
.venv/bin/pytest path/to/tests/test_file.py
```
