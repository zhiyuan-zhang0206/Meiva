# CLI argument discipline: parse-layer gates, retained defaults with reasons

How the `meiva` argparse surface treats a missing, malformed, or defaulted
argument. Read before adding or reviewing a CLI flag (user ruling 2026-09-20:
CLI arguments are fully explicit; tasks #4085 / #4092).

## The default: explicit, and gated at the parse layer

An invocation that cannot be honored is refused before any command code runs,
as a usage error — message on stderr, exit code 2. Three shapes:

- **Mandatory argument**: `required=True` on the argument, or a required
  mutually-exclusive group when exactly one source supplies the value.
- **Conditional requirement** (one of several flags must be present; X is only
  meaningful with Y): enforced in the verb's `_h_*` handler in `cli/parsers/` —
  the parse-layer boundary — never deep in the command body.
- **Value contract** (format, range, duration, JSON shape): a `type=` callable
  on the argument. It may lazily import the canonical validator from its owning
  module (`base.*`, `services.*`) and re-raise `ValueError` as
  `argparse.ArgumentTypeError`; never duplicate the validator.

No environment variable satisfies a CLI argument that was not passed — the
caller writes it out. Command bodies keep their own guards for programmatic
callers (SDK, internal steps, tests), which bypass argparse.

## Lifecycle verbs: no defaults, no inference

User ruling 2026-10-20: no automatic judgement and no default parameter;
everything is specified explicitly
([decision](../../decisions/runtime/updates/release/2026-10-20-explicit-lifecycle-choices.md),
which also records what each verb fixes by its own meaning). It covers every lifecycle verb — `meiva stop`, `meiva start`,
`meiva restart` and `meiva update run|down|up` — and the `cmd_*` /
`do_*` functions they call.

- **Every choice is a required argument.** A keep-or-stop choice is a pair of
  flags (`--keep-shell-sessions`/`--close-shell-sessions`,
  `--keep-browser`/`--stop-browser`, `--keep-extras`/`--stop-extras`,
  `--keep-infra`/`--stop-infra`, `--keep-service NAME…`/`--keep-no-services`,
  `--yes`/`--prompt`); a mode, a budget or a service selection is a required
  value. A missing choice is a usage error (exit 2) that names **every** missing
  choice at once (`refuse_usage` in `cli/parsers/host.py` and
  `cli/fleet_update/choices.py`), so an operator
  never discovers them one retry at a time.
- **Machine facts only refuse.** A host's capabilities decide which choices
  apply — a gateway requires `--keep-infra|--stop-infra`, any other host refuses
  them, unreadable capabilities refuse the verb — and how a step is carried out
  (macOS starts through a LaunchAgent). They never answer a choice for the
  operator. Nothing is inferred from a diff, the environment or an earlier run:
  `meiva start` persists no selection and a bare start is a usage error;
  `meiva lgtm on|off` and standalone `meiva converge` take the same selection.
- **Callers forward verbatim.** fleet update passes the operator's choices to
  each host's `meiva stop` / `meiva start` unchanged; internal callers pass
  keyword-only arguments that have no default.
- **No retained default.** The exception below does not apply to lifecycle
  verbs; none of them carries a `task #4092 cli-default inventory` marker.

## The exception: a retained default, with a reason

A flag may keep a default when writing it out would be noise — monitoring
verbs cron invokes bare, presentation bounds, safe-mode switches. Every
retained default carries a one-line reason comment at its definition site,
behind the marker:

```bash
git grep -n 'task #4092 cli-default inventory'
```

The marker *is* the inventory; there is no second list to keep in sync (the
same shape as the exception inventory in [numeric-limits.md](numeric-limits.md)).
The reason states why the default cannot be required and what fixes the value:
the cron payload, the display bound, the safe mode.

## Retained environment inputs

The worktree override deliberately stays environment-only:

- The worktree override in `cli/commands/extensions/skills_sync.py` — the default is
  the protection (never sync a worktree checkout's sources into a production
  home); the environment switch is the deliberate escape hatch for a caller
  that means it.

## Reading the parsed arguments: typed, once

`argparse.Namespace` attributes are `Any`, so a handler never reads `args.name`.
It declares a frozen dataclass of the attributes it reads and calls
`base.host.boundary.options.options(args, Shape)` as its first line; every later use holds a real
type. A field is the namespace attribute of the same name and carries its type
(`str`, `int`, `Path | None`, `list[str]`, `Literal[...]`, an enum, a domain class);
a field has no default, because the parser answered it. An attribute the namespace
lacks or one of the wrong type is an `OptionsError` naming every such field at once,
which is a programming error that raises: argparse has already checked what the operator
typed, so a mismatch means the parser and its handler disagree, and it fails the first
time the verb runs rather than being reported as a usage error.

The entry binds a few values beside the flags (`args.runtime`, `args.database_factory`,
`args.producer`, `args.site`). Each has a class and an accessor next to it
(`cli.database.runtime.runtime_of`, `cli.start_runtime.site_of`) that wraps
`base.host.boundary.options.attribute`. A dataclass is declared in the parser module and uses only
types that module can import without loading Settings; the accessor and the
`cmd_*` implementation are imported inside the handler, as before.

## The review action

A new CLI argument without `required` / `type=` needs a required form, a
parse-layer gate, or a marked retained default. An invocation that can only
fail after the command starts — a traceback, or a bare `return 1` on a missing
or malformed argument — is a request-changes. Usage-error tests live beside
the verb in `tests/components/cli/` and assert exit code 2 plus the message.
