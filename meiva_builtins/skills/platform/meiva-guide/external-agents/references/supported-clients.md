# Client skill directories Meiva publishes into

`meiva start` runs one host-global converge step that copies two complete skills —
`meiva-guide` and `impersonator-guide` — into the user-level skill directory of
each external agent CLI on the machine. This page is the evidence behind the
`_CLIENTS` registry in `cli/commands/extensions/external_skills.py`. When a
tool changes its layout, change the registry row and this table together.

## How Meiva decides a tool is present

A client counts as present when **its home directory exists or its program
resolves on `PATH`**. A surface with no program of its own — a desktop app — is
known only by its directory. With neither mark nothing is written, so a machine
without a tool never grows that tool's directory. An installed CLI that has
never run has no home yet; publication then creates `<home>` (mode `0700`)
together with `<home>/skills/`, because the tool's own first run would have done
it anyway.

Presence is not a claim about the tool's version. A layout change in a release
Meiva has never seen is still published to the old path: re-check this table
when a tool's major version changes.

## Matrix

| Tool | Program | Global skill directory | What lands there | Evidence | Verify |
|---|---|---|---|---|---|
| Codex | `codex` | `~/.codex/skills/` | `meiva-guide/`, `impersonator-guide/`, each with `.meiva-managed.json` | `codex` binary 0.157.1: `$CODEX_HOME/skills` paths, `codex_config::skills_config`, `app-server/src/skills_watcher.rs`; also reads `~/.agents/skills` | `ls ~/.codex/skills/meiva-guide/SKILL.md`; the tool's own skill listing was not confirmed on 0.157.1 (no `codex skills` subcommand) |
| Claude Code | `claude` | `~/.claude/skills/` | same two trees | pre-existing integration; `SKILL.md` loader in the `claude` binary | `ls ~/.claude/skills/meiva-guide/SKILL.md`; in-session skill listing |
| Gemini CLI | `gemini` | `~/.gemini/skills/` | same two trees | `@google/gemini-cli` 0.28.2 and 0.64.0: `Storage.getUserSkillsDir()` = `~/.gemini/skills`; `loadSkillsFromDir` globs `SKILL.md` and `*/SKILL.md` only, so the guide's nested sub-skills stay ordinary files; also reads `~/.agents/skills` | `gemini skills list` |
| Grok | `grok` | `~/.grok/skills/` | same two trees | `@xai-official/grok` 1.0.62, user guide embedded in the binary: "a skill at `~/.grok/skills/commit/SKILL.md`", `[skills] paths/ignore/disabled` in `~/.grok/config.toml`, `/skills` modal; also reads `~/.claude/skills` and `~/.cursor/skills` for compatibility | `/skills` inside `grok`; `ls ~/.grok/skills/meiva-guide/SKILL.md` |
| Qwen Code | `qwen` | `~/.qwen/skills/` | same two trees | `@qwen-code/qwen-code` 0.25.2: `Storage.getUserSkillsDir()` = `~/.qwen/skills`, scanner list `[".qwen", ".agents"]` | `ls ~/.qwen/skills/meiva-guide/SKILL.md`; `/skills` in a session |
| Cursor Agent | `cursor-agent` | `~/.cursor/skills/` | same two trees | `cursor.com/docs/context/skills`: "`~/.cursor/skills/` — User-level (global) on the local machine"; compatibility loads `~/.claude/skills/` and `~/.codex/skills/` | `/` menu in Agent chat; `ls ~/.cursor/skills/meiva-guide/SKILL.md` |
| Antigravity App + IDE | (none — desktop app) | `~/.gemini/config/skills/` | same two trees | `antigravity.google/docs/skills`: "Antigravity App skill locations" and "Antigravity IDE skill locations" both name `~/.gemini/config/skills/<skill-folder>/`; the IDE also reads the legacy `~/.gemini/antigravity/skills/`. Observed on macbook-air: `~/.gemini/config/skills/` already holds hand-installed skills | Customizations menu in the IDE agent panel; `/<skill-name>` in the App prompt panel |
| Antigravity CLI | `agy` | `~/.gemini/antigravity-cli/skills/` | same two trees | same doc, "CLI skill locations": `~/.gemini/antigravity-cli/skills/<skill-folder>/` (plus plugin-provided `.../plugins/<name>/skills/`) | `/<skill-name>` in the `agy` TUI; `agy plugin list --json` lists plugin-bundled skills |
| DeepSeek Harness | `dsh` | `~/.dsh/skills/` (`$DSH_HOME/skills`) | same two trees | `@deepseek-ai/dsh-skill-filesystem` 0.0.1-rc.3 discovery table: rank 400 `user-dsh` = `<dshHome>/skills`; `@deepseek-ai/dsh-base` 0.2.0-rc.2 profile loads that provider; `@deepseek-ai/dsh-home-paths` resolves `$DSH_HOME` then `~/.dsh` | `ls ~/.dsh/skills/meiva-guide/SKILL.md`; a `dsh` session lists the skill in its catalog and loads it with the `skill` tool |

Every published directory carries `.meiva-managed.json` (installation id,
generation id, source digest). A directory without a matching ledger under
`$MEIVA_HOME/configs/external-agent-skills/` is unmanaged: Meiva reports the
conflict and preserves it.

## Visibility, not ownership

The copies are read-only for Meiva after publication, and the user may edit or
delete any of them; an edited copy is preserved and re-reported, not overwritten.
Reload behaviour belongs to each tool — a session already running may need its
own reload (Gemini's `/skills reload`) before a new copy appears.

Several tools read their neighbours' directories for compatibility (Cursor:
`~/.agents/skills`, `~/.claude/skills`, `~/.codex/skills`; Grok: `~/.claude/skills`,
`~/.cursor/skills`, `~/.cursor/rules`; Gemini and Qwen: `~/.agents/skills`). The
same skill can therefore be visible twice on one machine. Both copies come from
one source snapshot, so their content is identical — but a user edit to one copy
does not reach the other, and one deleted copy can leave the other in place.

Meiva does not write `~/.agents/skills`. That directory is shared by several
tools and is not any one tool's home; publishing there would also duplicate every
skill for tools that already read their own directory.

### DeepSeek Harness is the official DeepSeek coding-agent CLI

`@deepseek-ai/dsh` is DeepSeek's own coding-agent CLI — published under the
official `@deepseek-ai` npm scope, repository
`deepseek-ai/deepseek-harness` (`apps/cli`), binary `dsh`. It is not a Meiva
wrapper: `spawn_dsh.py` in this guide only launches the installed CLI in a
persistent shell with Meiva's relay plugin. The surrounding Meiva code likewise
integrates the CLI rather than reimplementing one, so this single row covers both
"the official DeepSeek CLI" and the harness Meiva drives.

### Where the Antigravity desktop app keeps its own data

`~/.antigravity/` and `~/.antigravity-ide/` are the App and IDE data directories —
`argv.json`, `extensions/` and the like. They are not skill directories and Meiva
does not write to them; the skills live under `~/.gemini/` as the table shows.
Both were checked on macbook-air, where the user had previously copied
`~/.antigravity/extensions` into `~/.antigravity-ide/`.

## Not supported

- **Antigravity legacy global directory.** `~/.gemini/antigravity/skills/` is
  still read by the IDE as a compatibility path, but it is not where current
  releases point; Meiva writes the supported location only.
- **Aider.** No skill directory: conventions live in a `CONVENTIONS.md` the user
  adds to the chat (`/read CONVENTIONS.md`, `aider --read`). Meiva has no
  single-file instruction mapping for it, and the guide is too large to inline.
- **Single-file instruction files** (`GEMINI.md`, `AGENTS.md`, `QWEN.md`,
  `CLAUDE.md`). Every supported tool has a native skills directory, so the
  fallback is not needed and is not implemented.

## Unconfirmed

- **Grok** and **Cursor Agent** were verified from published packages and
  documentation, not from an installed CLI on a Meiva host
  (`@xai-official/grok` 1.0.62 embedded guide; `cursor.com/docs/context/skills`).
  Their first real publication is the confirmation.
- **Gemini CLI** on the host that runs it is 0.28.2, which predates the current
  release; the layout is unchanged in 0.64.0, but a future major release may move
  it.
- **Codex** has no `skills` subcommand to list what it loaded; the directory is
  confirmed, the in-app listing is not.
- **`$DSH_HOME`.** The DeepSeek Harness row publishes to `~/.dsh/skills`, the
  default home. A machine that exports `DSH_HOME` elsewhere resolves skills from
  `<DSH_HOME>/skills`; that value is environment-dependent, so Meiva does not
  follow it (`$DSH_HOME` unset is the shipped default).
- **Antigravity CLI home.** Verified from the documentation, not from a machine
  on which `~/.gemini/antigravity-cli/` already existed: macbook-air has `agy` on
  `PATH` and the directory had not been created yet.
- **DeepSeek Harness repository documentation.** `github.com/deepseek-ai/deepseek-harness`
  is not readable anonymously (404 to an unauthenticated fetch), so the evidence
  is the published npm packages and their profile files rather than the repository
  README. `dsh` is a developer preview (`0.2.0-rc.2`), so its skill roots may move.
