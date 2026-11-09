# Meiva — Documentation Index

Index of Meiva's package and domain documentation. Each package documents its design in an optional `<package>/README.md` (when a single overview suffices) or in standard Markdown files under `<package>/docs/` beside the source code it describes. Cross-domain concept overviews live under top-level `docs/`, chosen design alternatives live in `docs/decisions/`, incident records live in `docs/postmortems/`, and unimplemented plans live in `future/`.

## Domains

| Domain | Description |
|---|---|
| [Agent](../agent/docs/agent.md) | Agent runtime — LangGraph execution graph, state channels, messages, process lifecycle, and infrastructure such as DB / cache / MCP daemon |
| [Meiva](../meiva/docs/meiva.md) | SDK surface — `meiva.*` single namespace: files / shell / agents / task / memory / skills / mcps / web / self / ui / watcher |
| [Gateway](../gateway/docs/gateway.md) | Gateway — HTTP API, SSE push, authentication, `/api/agents` derived endpoints, bootstrap handshake |
| [CLI](../cli/docs/cli.md) | CLI — cluster lifecycle: start / stop / restart / update / converge, multi-machine deployment orchestration |
| [User interfaces](../ui/README.md) | User interfaces — shared Next.js web console (`ui/web`) and cross-platform Tauri native host shell (`ui/app` for macOS and Android) |
| [Built-ins](../meiva_builtins/docs/meiva_builtins.md) | Built-ins — bundled plugins, skills and MCP integrations |
| [Base Library](../base/docs/base.md) | Base library — lm provider abstraction, agents-contract wire protocol, migrations, logging, configuration |
| [Background services](../services/docs/services.md) | Background services — long-running subsystems such as browser MCP wrapper |
| [.github](../.github/README.md) | GitHub Actions surface — backend/frontend/e2e and shell CI, cluster/app releases, issue + PR templates |
| [Plugin System](plugins/plugins.md) | Plugin system — extension mechanism that can carry skills / MCP servers |
| [Skill System](skills/skills.md) | Skills — pure markdown instruction bundles, lazily loaded by agents on demand |
| [MCP Integrations](mcps/mcps.md) | MCP integration — built-in chrome + external servers installed via `meiva mcp install` (e.g., x), invoked via daemon subprocesses |
| [Test Suite](../tests/docs/tests.md) | Test suite — pytest unit / integration / e2e tests covering all modules |
| [Scripts](../scripts/docs/scripts.md) | Operations scripts — lint / build / install / provision / CI / release toolset |
| [meizel/ — Locality Rules](../meizel/docs/meizel.md) | Locality rules — shared lint scope, file and directory budgets, Python language restrictions |

## Maintenance

Keep package documentation accurate in the same PR as the code change. `scripts/content_lint/check_doc_references.py` validates CLI flags and relative Markdown links across tracked documentation. See [conventions/doc-maintenance.md](conventions/doc-maintenance.md).
