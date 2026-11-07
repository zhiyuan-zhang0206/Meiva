"""Meiva code plugin — coding-agent conventions + automatic context-file injection.

Two pieces, cwd and context-file injection:
- cwd: `meiva_code__cwd` state field (default: the agent's workspace dir; $HOME
  when no process identity is bound); the agent reads/writes via
  `meiva.cwd.get` / `set`; the wraps use it to resolve relative paths in
  `meiva.files.*` / `meiva.shell.run` / each `meiva.understand` target's `paths` /
  `meiva.ui.serve`'s `dir`.
- context-file auto-injection: wraps `meiva.files.read`, walks up the resolved
  path to git root or `$HOME` (whichever is farther), and surfaces any AGENTS.md
  / CLAUDE.md files along the way as a system note (tag=CONTEXT) so the agent
  sees them next turn. Delivery is **in-memory, inside the exec turn** (user
  ruling 2026-08-11 — the old side-channel JSONL file is gone): the wrap
  appends each note to the base `messages` channel via `update_state`,
  and the exec node (agent/graph/exec/node.py) merges the plugin's messages delta
  with its own exec-result ToolMessage, after it (the Anthropic-compat wire
  contract forbids notes between the AIMessage and its ToolMessage). No file
  is written except the overflow archive for oversized context files
  (truncated head+tail inline, full text under the workspace `.exec_output/`
  ring — the same logic as exec output overflow). **The wrap is a fallback**:
  the system prompt tells the agent to first `meiva.files.read("AGENTS.md")`;
  when the agent goes through that primary path, the wrap marks that path into
  `injected_paths` but does not inject it (the content is already returned to
  the agent). Dedup uses `injected_paths: set[str]` plugin state; after compact
  strips messages, it is lazily reset via `last_seen_compact` <
  `compact.version`.

When the wrap is called outside an exec turn (test / dev REPL), it fast-paths
straight through to the original read — no path rewriting, no injection.
Behavior is identical to the original SDK function, avoiding a silent fallback
to the system cwd that would diverge from plugin-enabled behavior.

This module is the plugin's SDK **surface** — the only face an agent-launched
child loads (task #3633). Its agent-runtime registrations (the `meiva_code__cwd`
state field, the two system-prompt sections, the after_init / after_exec
hooks) live in `agent_runtime.py`, imported only on the full path (see
`agent/extensions/__init__.py`); `read_state` / `update_state` (`_state_access.py`) load it on first use
and raise the same `PluginStateOutsideTurnError` the real handle raises
outside an exec turn.
"""

from __future__ import annotations

import hashlib

__description__ = "Meiva Code conventions — maintains cwd and auto-injects project AGENTS.md / CLAUDE.md (walking up from meiva.files.read paths)"

from collections.abc import Callable
from pathlib import Path

import meiva.files as _meiva_files_mod
from base.log import logger
from base.packages.plugins.extensions import PluginContributions, SdkWrap
from base.packages.skills import project_skill_roots
from meiva.sdk_surface import process_context
from meiva.sdk_surface.agent_identity import require_agent_id
from meiva.sdk_surface.validation import coerce_str

from ._state_access import read_state, update_state
from ._walk import find_context_files_along_path

_files_resolve = _meiva_files_mod.resolve


# ── project-local skill source ───────────────────────────────────────────
# Contribute the working repo's project-skill folders (resolved from cwd at
# scan time) so they surface under `meiva.skills.*` like any other skill. The
# scan runs on every lookup AND once at the system-prompt build, where state
# is not yet bound (cwd still default) — there we contribute nothing.
def _project_skill_source() -> list[Path]:
    if not process_context.in_exec_turn():
        return []
    from meiva import cwd

    return project_skill_roots(cwd.get())


# ── wrap meiva.files.read ───────────────────────────────────────────────────
# Declared as an `SdkWrap` in `contribute()` at the bottom. The
# `inner` parameter is the current `meiva.files.read` (the original, or another
# plugin's wrap when several stack); context-file dedup counts injections, not
# wrap layers, so calling `inner` exactly once per read keeps the count right no
# matter how many layers sit below.


def _process_context_file(
    ctx_file: Path,
    target: Path,
    *,
    injected: set[str],
    hashes: set[str],
    max_chars: int,
) -> bool:
    """Process one candidate context file for auto-injection.

    Returns True when the caller should continue to the next candidate
    (file was handled: injected, skipped via dedup, or errored).
    Returns False when the file was not a context file (should not happen).
    """
    # Agent-runtime half only: reachable when a live state slot exists
    # (a stateful child / the agent process), so these imports stay off the
    # surface boot (task #3633).
    from agent.graph.exec.output import overflow_dir, truncate_both_ends
    from agent.messages import NoteTag, system_note_message
    from meiva.sdk_surface import process_context
    from meiva.sdk_surface import settings as sdk_settings
    from meiva.security import scan_content

    ctx_file_str = str(ctx_file)
    if ctx_file_str in injected:
        return True
    # Primary-path check: if the target read is this context file itself,
    # the content reaches the agent via the return value — do not auto-inject.
    # Still mark into injected to prevent a later sibling read from injecting,
    # and hash the content so a same-content copy from another path is also blocked.
    try:
        is_target = ctx_file.samefile(target)
    except OSError:
        is_target = False
    if is_target:
        injected.add(ctx_file_str)
        try:
            target_content = ctx_file.read_text(encoding="utf-8")
        except (PermissionError, FileNotFoundError, UnicodeDecodeError):
            return True
        hashes.add(hashlib.sha256(target_content.encode()).hexdigest())
        return True
    try:
        content = ctx_file.read_text(encoding="utf-8")
    except (PermissionError, FileNotFoundError) as e:
        logger.warning("[meiva_code] skip context file {p}: {e}", p=ctx_file, e=e)
        return True
    except UnicodeDecodeError as e:
        logger.warning("[meiva_code] context file {p} is not utf-8: {e}", p=ctx_file, e=e)
        return True
    # Skip empty files — no content to surface to the agent.
    if not content.strip():
        return True
    # Hash-based dedup: same content from a different path must not be
    # injected twice (the worktree vs main-repo AGENTS.md case).
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    if content_hash in hashes:
        injected.add(ctx_file_str)
        return True
    # Injection-pattern scan: records a SECURITY finding (delivered as a
    # system note by the exec node) when the content carries injection
    # patterns; the content itself is returned clean.
    scan_content(content, source=f"context-file:{ctx_file}")
    # Deliver the context file as a system note in THIS exec's messages
    # delta (in-memory — no side-channel file). Oversized content is
    # truncated head+tail with the full text archived to the workspace
    # .exec_output/ ring, same logic as the exec node's output overflow
    # (user ruling 2026-08-11: files only for archiving / oversized
    # content); the archive path rides in the injected note.
    note_body = content
    if len(note_body) > max_chars:
        clock = sdk_settings.clock()
        note_body = truncate_both_ends(
            note_body,
            max_chars,
            archive_dir=overflow_dir(
                require_agent_id(), home=process_context.current_context().require_meiva_home()
            ),
            now=clock.now().astimezone(clock.explicit_zone()),
        )
    now = sdk_settings.clock().now()
    update_state(
        {
            "messages": [
                system_note_message(
                    content=f"Project {ctx_file.name} from {ctx_file_str}:\n\n{note_body}",
                    tag=NoteTag.CONTEXT,
                    picked_up_at=now,
                    created_at=now,
                )
            ]
        }
    )
    hashes.add(content_hash)
    injected.add(ctx_file_str)
    return True


def _wrapped_read(
    inner: Callable[..., str],
    path: str | Path,
    start: int | None = None,
    end: int | None = None,
    *,
    limit: int | None = None,
    with_line_numbers: bool = False,
) -> str:
    """Read a file, or a 1-indexed inclusive line range.

    `limit` (max lines from `start`) is mutually exclusive with `end`.
    Project `AGENTS.md` / `CLAUDE.md` files found along the resolved path
    are surfaced as a system note once each.
    """
    # Audience=agent. Dev-perspective implementation (cwd resolution /
    # context-file walk / dedup / fast-path) is documented in plugin.py's
    # module docstring. This wrapper takes over `meiva.files.read`'s namespace
    # entry; the docstring above (kept by the wrap install) is the read
    # contract. `inner` is the wrapped `meiva.files.read`.

    # Normalize before path rewriting: a trailing-comma tuple must become
    # the string it wraps, or the cwd resolution below would str() it into
    # a garbage filename.
    path = coerce_str(path, "path", allow_types=(Path,))

    # Fast-path: when called outside a turn (test / dev REPL), pass through
    # to the wrapped read — no path rewriting, no injection.
    if not process_context.in_exec_turn():
        return inner(path, start, end, limit=limit, with_line_numbers=with_line_numbers)

    p = _files_resolve(path)

    # 2. Walk up the path collecting context files (AGENTS.md / CLAUDE.md)
    candidates = find_context_files_along_path(
        p, home=process_context.current_context().require_user_home()
    )

    # 3. Lazy compact reset: each successful compaction bumps compact.version.
    # This wrap entry compares against the bookmark; if the version advanced,
    # clear injected_paths — the corresponding context-file content in messages
    # has been replaced by a summary and needs to be re-surfaced to the agent.
    # compact is a built-in BaseAgentState sub-state (always present), so read
    # it directly — a missing value is a real bug, not a default-to-0 case.
    from agent.state import compact_version

    current = read_state()
    compact_v = compact_version()
    if compact_v > current.last_seen_compact:
        injected: set[str] = set()
        hashes: set[str] = set()
        new_bookmark = compact_v
    else:
        injected = set(current.injected_paths)
        hashes = set(current.injected_hashes)
        new_bookmark = current.last_seen_compact

    # 4. Inject / mark walk results. For multiple reads in the same turn,
    # read_state() reflects values updated earlier (update_state
    # synchronously mutates the meiva.state working copy); no extra cache needed.
    from meiva.sdk_surface import settings as sdk_settings

    raw_max: object = sdk_settings.agent_setting("exec_output_max_chars")
    max_chars = raw_max if isinstance(raw_max, int) and not isinstance(raw_max, bool) else 0
    for ctx_file in candidates:
        _process_context_file(ctx_file, p, injected=injected, hashes=hashes, max_chars=max_chars)

    # 5. Commit state. injected_paths has no reducer, so it follows
    # last-value semantics — the wrap is the sole writer, so just pass the
    # full new set.
    update_dict: dict[str, object] = {}
    if injected != current.injected_paths:
        update_dict["injected_paths"] = injected
    if hashes != current.injected_hashes:
        update_dict["injected_hashes"] = hashes
    if new_bookmark != current.last_seen_compact:
        update_dict["last_seen_compact"] = new_bookmark
    if update_dict:
        update_state(update_dict)

    return inner(str(p), start, end, limit=limit, with_line_numbers=with_line_numbers)


def contribute() -> PluginContributions:
    """What this plugin declares for the SDK surface."""
    return PluginContributions(
        sdk_wraps=(SdkWrap("files.read", _wrapped_read),),
        skill_sources=(_project_skill_source,),
    )
