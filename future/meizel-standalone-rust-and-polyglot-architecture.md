# Meizel: Bounded Container Topology, Purity Ratchets, and Standalone Polyglot Architecture

> **Status**: Python `meizel` purity and bounded-container rules (`none` / `strict-to-be` / `strict`, 15 deterministic codes) are implemented and dogfooded on `meizel/`.
> **What's left**: (1) Repository-wide rollout of `strict` and `strict-to-be` across `base/`, `ops/`, `agent/`, `gateway/`, `cli/`, `meiva/`, and `services/`; (2) tightening `basedpyright` (`reportAny`, `reportUnknown*`) with `typings/` stubs for untyped third-party boundaries; (3) adding TypeScript (`ui/web/src/`) and standalone Rust (`meizel-rs`) polyglot frontends over the shared `meizel.toml` / `meizel.policy.toml` contract.

---

## 1. Why LLM Agents Require Bounded Container Topology Instead of Heuristic Ratios

Traditional static analysis tools (Pylint, SonarQube, Radon Maintainability Index) rely on **ratios** and **local per-function thresholds** (for example, comment-to-code ratio, average cyclomatic complexity per function, or abstractness-vs-instability scores). Under an autonomous coding agent, ratio-based rules fail predictably:

- **Function-splitting gaming**: When a linter caps per-function lines or branches without bounding the enclosing module, an agent splits one 120-line function into six 20-line private helpers in the same file, threading 9 parameters between them. Local complexity drops while module coupling and cognitive load increase.
- **Pass-through & re-export gaming**: When a linter penalizes cross-package imports without checking whether an intermediate symbol does real work, an agent adds 3-line pass-through wrappers (`def f(a, b): return g(a, b)`) or wide `exports` lists to appease the import check.
- **Monkeypatch gaming**: When a function reads ambient state (`os.environ`, `datetime.now`, module-level singletons) or invokes effectful I/O mid-calculation, the shortest path to a green test is `monkeypatch.setattr(...)` on an imported module attribute — coupling the test to internal implementation details and inflating the test suite with brittle setup.

Meizel replaces ratios with **Bounded Container Topology** — hard, orthogonal caps at every level of the containment hierarchy so squeezing complexity out of one level forces genuine structural decomposition at the boundary above it:

| Containment Level | Hard Budget | Default | What Happens When Exceeded |
|---|---|---|---|
| **Directory** | `max-dir-entries` (`budgets/limits.py`) | 20 | Must split into cohesive subpackages with explicit `meizel.toml` doors |
| **Package Door (`meizel.toml`)** | `wide-exports` (`max-exports`) & `stale-export` | 4 | Must expose a narrow public door (`__init__.py` or $\le 4$ submodules); unused exports are pruned |
| **Module Dependency Fan-Out** | `wide-fanout` (`max-uses`) | 8 | A module depending on $> 8$ sibling targets must split orchestration from domain logic |
| **File Size** | `max-file-lines` (`budgets/limits.py`) | 800 | File must split along domain responsibilities |
| **Module Public Surface** | `wide-public-surface` (`max-public-symbols`) | 6 | At most 6 public behavioral symbols (`def` / stateful `class`) per module; frozen `@dataclass(frozen=True)` value objects, `Enum`s, `Protocol`s, `TypedDict`s, `NamedTuple`s, and `Exception`s are free so extracting typed domain values is rewarded |
| **Function Signature** | `wide-params` (`max-params`) | 6 | Functions taking $> 6$ parameters (excluding `self`/`cls`) must group cohesive parameters into a `@dataclass(frozen=True)` value object |
| **Expression / Statement** | `deep-attr-chain` (`max-attr-hops`), `nested-attr-write`, `pass-through-function` | 3 hops | Forbids train-wreck navigation (`a.b.c.d`), deep attribute mutation (`a.b.c = x`), and zero-value wrapper functions |

Because frozen value classes do not count toward `wide-public-surface`, the easiest way for an agent to satisfy `wide-params`, `untyped-export`, and `wide-public-surface` simultaneously is to define small `@dataclass(frozen=True)` domain types and pure functions over them.

---

## 2. The Three-Level Purity Contract (`none` / `strict-to-be` / `strict`) and Debt Ratchet

A repository of hundreds of thousands of lines cannot freeze feature delivery to purify every module at once, nor can a linter that only warns prevent regressions. Meizel solves this with a three-level purity contract declared in `meizel.toml` (`purity = "strict"` or per-child `[purity]`) and backed by the shrink-only `[debt.<code>]` ledger:

1. **`none` (Imperative Shell / Unmigrated)**:
   - Enforces the nested DAG locality rules (`undeclared-edge`, `not-exported`, `private-module`, `private-name`, `cycle-edge`), `restricted-python`, and patch debt (`first-party-patch`, `stdlib-patch`, `third-party-patch`, `env-patch`, `unresolved-patch`, `missing-patch-target`).
   - Also distinguishes **imported-binding patches** (`monkeypatch.setattr(mod, "name", ...)` where `mod` only imported `name` from another module) so fragile patches are surfaced immediately.
2. **`strict-to-be` (Ratcheted Migration)**:
   - Evaluates all 15 purity and bounded-container rules (`impure-edge`, `impure-call`, `unfrozen-value`, `untyped-export`, `pass-through-function`, `nested-attr-write`, `domain-subset-leak`, `stale-export`, `wide-exports`, `wide-fanout`, `wide-public-surface`, `wide-params`, `deep-attr-chain`, `impure-test-fixture`, `nested-patch-target`).
   - Existing violations are recorded in the directory's `meizel.toml` via `python -m meizel debt record <paths...>`.
   - Any **new** violation fails `meizel check` immediately.
   - Any **repaid** violation triggers `debt-paid` until `python -m meizel debt prune` lowers the baseline.
   - When a `strict-to-be` file or directory reaches **zero** remaining purity violations and zero recorded purity debt, `meizel check` emits `debt-paid` requiring promotion to `"strict"`.
3. **`strict` (Functional Core)**:
   - Zero purity or container-budget debt is permitted (`_purity_debt` returns `None`).
   - A `strict` module cannot import a `none` module (`impure-edge`), cannot define `async def` or invoke ambient clocks/env/randomness/filesystem/network I/O (`impure-call`), cannot define mutable classes or module-level mutable state (`unfrozen-value`), and cannot expose `Any` or bare `dict` on its public signatures (`untyped-export`).
   - **Automatic Test Purity Inheritance**: Any test file whose first-party targets under test are all `strict` (or `strict-to-be`) is automatically barred from requesting `monkeypatch`, `mocker`, or heavy I/O fixtures (`impure-test-fixture`) and from patching chained attributes (`nested-patch-target`). Once the production module is pure, its unit tests are forced to be pure value-in / value-out assertions.

---

## 3. Sealing the Python Type Boundary vs. Rewriting Application Code in Rust

A common question when hardening a Python codebase for agentic engineering is whether to rewrite the application in Rust for compiler-enforced mutability and type guarantees. For an I/O-bound, schema-heavy agent platform like Meiva, a full application rewrite in Rust has poor economics compared to sealing Python's boundaries mechanically:

- **Where Python leaks**: Standard Python allows unannotated `Any`, bare `dict`, mutable dataclasses, and ambient side effects.
- **How Meizel + Basedpyright close 95% of the gap**:
  1. `meizel` enforces `@dataclass(frozen=True)` / `BaseModel(frozen=True)` (`unfrozen-value`), forbids `Any` and bare `dict` on public signatures (`untyped-export`), and bans side effects in `strict` modules (`impure-call`, `impure-edge`).
  2. `basedpyright` configured with `reportAny`, `reportUnknownParameterType`, `reportUnknownReturnType`, `reportUnknownMemberType`, and `reportUnknownVariableType` propagates type checking inside function bodies.
  3. Untyped or loosely typed third-party boundaries (such as LangChain message dicts) are isolated behind single-file narrowing adapters (like `base/lm/content.py`) or local `typings/` `.pyi` stubs rather than leaking `Any` across domain packages.

By contrast, **`meizel` itself** is a pure AST + graph analyzer that runs on every pre-commit, pre-push, and CI job across repositories. Extracting `meizel` into a standalone Rust binary (`meizel-rs`) yields a $10\text{--}20\times$ speedup (sub-50ms full-repository checks) and zero-runtime-dependency distribution across Python, TypeScript, and polyglot repositories.

---

## 4. The Cross-Language Rule Matrix (75% Universal / 20% Compiler-Polyfill / 5% Language Quirks)

Meizel's architecture cleanly separates the **language-agnostic graph & debt engine** (`meizel/graph/`, `meizel/contracts/`) from the **language-specific AST scanner** (`meizel/language/`). Across programming languages, the rules partition into three tiers:

### Tier 1: Universal Topological & Bounded-Container Rules (~75% — Identical Across All Languages)

These rules operate on `FileScan` (`Fact` edges, exported symbols, parameter counts, attribute chains, and effectful imports/calls) and require **zero changes** to the core DAG/debt engine:

| Rule | Python | TypeScript (`.ts` / `.tsx`) | Rust | Go | Kotlin / Java |
|---|---|---|---|---|---|
| **Nested DAG Locality** (`undeclared-edge`, `not-exported`, `private-module`, `private-name`, `cycle-edge`) | `import` / `from ... import` | `import ... from` / `export ... from` (`./`, `../`, `@/`) | `use crate::...` / `mod` | `import "pkg/..."` | `import pkg...` |
| **Container Budgets** (`wide-exports`, `stale-export`, `wide-fanout`, `wide-public-surface`, `wide-params`) | `exports`, `[uses]`, `def` / `class` | `exports`, `[uses]`, `export function` / `export class` (excluding `type` / `interface`) | `pub fn` / `pub struct` (with `impl`) | Exported `func` / receiver methods | `public` methods / classes |
| **Structural Shape** (`pass-through-function`, `deep-attr-chain`, `nested-attr-write`, `domain-subset-leak`) | AST `Call` / `Attribute` / `Enum` | TS AST property chains & enum subsets | Method/field chains & `match` subsets | Selector chains & `switch` subsets | Chained getters/setters & `when`/`switch` |
| **Purity Boundary** (`impure-edge`, `impure-call`, `impure-test-fixture`, `nested-patch-target`) | `async def`, `os.environ`, `datetime.now`, `Path.*`, `monkeypatch` | `async function`, `fetch`, `localStorage`, `window`, `Date.now`, `process.env`, `vi.spyOn` / `vi.mock` | `async fn`, `std::fs`, `std::env`, `std::net`, `SystemTime::now` | `os.*`, `net/http`, `time.Now` | `System.getenv`, `Instant.now`, `Mockito.mock` |

### Tier 2: Compiler-Polyfill Rules (~20% — Essential in Dynamic/Gradual Languages, Native in Static Compilers)

| Rule | Python | TypeScript | Rust / Go / Kotlin |
|---|---|---|---|
| `untyped-export` | Forbids missing annotations, `Any`, bare `dict` | Forbids missing return types, `: any`, `as any`, `Record<string, any>` | Enforced natively by the compiler |
| `unfrozen-value` | Requires `@dataclass(frozen=True)`, `NamedTuple`, `Enum` | Requires `readonly` properties / `Readonly<T>` / `as const` | Enforced natively in Rust (immutable by default); `val` in Kotlin |
| `missing-patch-target` / `*-patch` | `monkeypatch.setattr`, `unittest.mock.patch` | `vi.spyOn`, `vi.mock`, `jest.spyOn`, `Object.defineProperty` | N/A in Rust/Go (no runtime module monkeypatching) |

### Tier 3: Language-Specific Quirks (~5%)

- **Python**: `restricted-python` (`importlib`, `sys.modules` mutation, `exec`), `if TYPE_CHECKING:` discipline, `pytest` fixture resolution (`conftest.py`, `pytest_plugins`).
- **TypeScript**: Barrel file (`index.ts`) re-export chains, path aliases (`tsconfig.json` `paths`), JSX component prop drilling.

---

## 5. Roadmap: Rollout in Meiva and Standalone `meizel-rs`

### Phase 1: Meiva Python Rollout (In Progress)
1. **Lock in already-pure modules (`strict`)**: Mark the ~570+ existing zero-violation Python modules across `base/`, `ops/`, `agent/`, `gateway/`, `cli/`, `meiva/`, and `services/` as `purity = "strict"` in their `meizel.toml` manifests so they can never regress.
2. **Ratchet transitional packages (`strict-to-be`)**: Mark near-pure domain packages as `strict-to-be` and record their baseline in `[debt.<code>]`.
3. **Burn down `monkeypatch` via pure seam extraction**: Convert `strict-to-be` modules with 1–3 purity violations into pure `strict` modules by injecting clocks, environment mappings, or value inputs, and delete `monkeypatch` from their unit tests (`debt-paid` $\to$ `strict`).
4. **Tighten `basedpyright` on `strict` packages**: Enable `reportAny` and `reportUnknown*` on `strict` directories and add `typings/` stubs for untyped third-party boundaries.

### Phase 2: TypeScript Frontend (`ui/web/src/`) and External Repo Assessment
1. Add a TypeScript scanner (`typescript.py` / `tree-sitter-typescript` in `meizel-rs`) resolving relative (`./`, `../`) and alias (`@/`) imports, barrel `index.ts` doors, `vi.spyOn` / `vi.mock` patch debt, and the 15 purity/container-budget rules.
2. Apply `purity = "strict"` to pure frontend domain libraries (`ui/web/src/i18n/`, `ui/web/src/lib/markdown/`, pure state reducers and timeline transforms) and `strict-to-be` to transport/store hooks.

### Phase 3: Standalone Open-Source Rust Tool (`meizel-rs`)
1. Port the `meizel` graph, manifest, ledger, and rule engine to a standalone Rust workspace (`meizel-rs`) reading the identical `meizel.toml` and `meizel.policy.toml` schema.
2. Use `ruff_python_ast` (or `rustpython-parser`) for Python and `oxc_parser` / `swc_ecma_parser` for TypeScript/TSX to achieve $< 50\text{ ms}$ full-repo analysis with zero runtime dependencies.
3. Publish as a standalone binary and pre-commit / CI action usable across Meiva, MonsoraV2, and external polyglot repositories.
