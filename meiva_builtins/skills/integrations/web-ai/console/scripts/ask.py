"""web-ai console — ask the same question to several frontier models at once.

The agent runs this as a subprocess, e.g.

    .venv/bin/python skills/web-ai/console/scripts/ask.py --prompt "<hard question>"
    .venv/bin/python skills/web-ai/console/scripts/ask.py --models chatgpt,gemini --prompt "..."
    echo "<long multi-line question>" | .venv/bin/python skills/web-ai/console/scripts/ask.py

and reads the JSON printed to stdout. It opens a fresh chat per model in the
user's logged-in browser, types the prompt into each, and then waits on them all
at once — every model streams its answer concurrently in its own tab, so the
wall-clock is the slowest single answer, not the sum. The combined answers land
at `~/Downloads/meiva_<cluster>_web-ai/console/<stamp>-<slug>/` (answers.md +
result.json) and the same structure is printed to stdout so the agent can read
and synthesize without opening a file.

This spends no API credits — it uses the flat-rate web subscriptions the user
already pays for. Each model answers in its own fresh conversation, independent
of the others.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from base.host.boundary.options import options
from base.paths.home import resolve_meiva_home, user_home

# `webchat` lives in the parent skill's scripts/ dir, a sibling tree within
# this same web-ai skill (not an importable package; PYTHONSAFEPATH=1 keeps a
# script's own directory off sys.path). Structure Rule 6 recognizes this exact
# __file__-derived, within-skill shape as the endorsed guard.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

import webchat
from _rows import AskRow, FailedRow

_DEFAULT_MODELS = ["chatgpt", "gemini", "claude"]


# meiva:note
# The row the console prints and lands in `result.json` for a model that answered: how it was
# submitted, the answer captured, whether that answer finished, and the conversation handle to
# resume it. `ok` is False for an answer that never landed (empty text), even when it finished.
class ConsoleAnswer(TypedDict):
    site: str
    label: str
    ok: bool
    complete: bool
    chars: int
    url: str
    chat_id: str | None
    tab_kept: bool
    submitted_via: str
    answer: str


# meiva:note
# The row the console prints for a model whose setup failed. It is a row instead of a raise so one
# dead model never sinks the panel's JSON.
class ConsoleFailure(TypedDict):
    site: str
    label: str
    ok: bool
    error: str


# meiva:note
# One row of the console's answer table: an answered model, or one the console could not set up.
ConsoleRow = ConsoleAnswer | ConsoleFailure


# meiva:note
# The flags this entry reads, as argparse left them. `options` checks each against its annotation
# once, so nothing below reads the `Any` a `Namespace` attribute would hand it.
@dataclass(frozen=True)
class _Options:
    prompt: str | None
    models: str
    timeout: float
    file: str | None
    continue_url: str | None
    chat_id: str | None
    keep_tab: bool


def _slug(text: str, *, max_len: int = 40) -> str:
    """A short filesystem-safe slug from the prompt's first words."""
    # meiva:note
    # `re.sub` answers a `str`; `re.findall` answers `list[Any]` (the stub cannot see that the
    # pattern has no groups), and joining that list is the unchecked path this rule closes. Both
    # spell the same tokens: the alphanumeric runs, lowercased.
    words = re.sub(r"[^A-Za-z0-9]+", " ", text.lower()).split()
    slug = "-".join(words)[:max_len].strip("-")
    return slug or "query"


def _to_row(row: AskRow | FailedRow) -> ConsoleRow:
    """Map one `webchat.ask_many` row to the console result row the agent reads.

    A setup-failure row (carrying `error`) becomes an `ok: False` row. A completed
    row is `ok` only when it has text AND finished: `ok` requires completion, not
    just text, since a timed-out run can hand back a transient placeholder (e.g.
    ChatGPT's "Thinking") as the partial.
    """
    # meiva:note
    # `error` is the failure row's own key: a row that has it is a `FailedRow`, one that does not is
    # an `AskRow` (an answer that never landed keeps its full row and reports `ok: False`).
    if "error" in row:
        return {
            "site": row["site"],
            "label": row["label"],
            "ok": False,
            "error": row["error"],
        }
    return {
        "site": row["site"],
        "label": row["label"],
        "ok": bool(row["answer"]) and row["complete"],
        "complete": row["complete"],
        "chars": len(row["answer"]),
        "url": row["url"],
        "chat_id": row["chat_id"],
        "tab_kept": row["tab_kept"],
        "submitted_via": row["submitted_via"],
        "answer": row["answer"],
    }


def _render_markdown(prompt: str, results: Sequence[ConsoleRow]) -> str:
    lines = ["# Console — multi-model answers", "", "## Question", "", prompt, ""]
    for r in results:
        lines += ["---", "", f"## {r['label']}"]
        if "error" in r:
            lines += ["", f"_failed: {r['error']}_", ""]
            continue
        if not r["ok"]:
            lines += ["", "_failed: no answer_", ""]
            continue
        if not r["complete"]:
            lines += ["", "_(answer did not finish within the timeout — partial)_"]
        lines += ["", r["answer"], ""]
    return "\n".join(lines)


def _save(prompt: str, results: Sequence[ConsoleRow], places: webchat.Places) -> Path:
    outdir = webchat.downloads_root("console", places=places) / f"{_run_stamp()}-{_slug(prompt)}"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "answers.md").write_text(_render_markdown(prompt, results), encoding="utf-8")
    (outdir / "result.json").write_text(
        json.dumps({"prompt": prompt, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return outdir


def _read_prompt(arg: str | None) -> str:
    if arg is not None:
        return arg
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    sys.exit("no prompt: pass --prompt '...' or pipe the question on stdin")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ask several frontier models the same question.")
    parser.add_argument("--prompt", help="the question; if omitted, read from stdin")
    parser.add_argument(
        "--models",
        default=",".join(_DEFAULT_MODELS),
        help=f"comma-separated subset of {', '.join(webchat.SITES)} (default: all)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="seconds to wait for each model's answer to finish (default 180)",
    )
    parser.add_argument(
        "--file",
        help="attach a local file to the question (sent to every model asked)",
    )
    parser.add_argument(
        "--continue-url",
        help="continue an existing conversation by URL instead of opening a fresh chat "
        "(requires exactly one model — the conversation belongs to one site). "
        "Prefer --chat-id with the chat_id from a previous result.",
    )
    parser.add_argument(
        "--chat-id",
        help="continue an existing conversation by chat ID (from a previous result's "
        "chat_id field) instead of opening a fresh chat. Exactly one --models site required.",
    )
    parser.add_argument(
        "--keep-tab",
        action="store_true",
        help="leave each model's browser tab open after answering (default: close "
        "it — the answer is already captured here; resume later with --continue-url <url>)",
    )
    return parser


def _continue_url(opts: _Options) -> str | None:
    """The conversation to continue: --continue-url, or --chat-id resolved via the first model's site."""
    continue_url = opts.continue_url
    if opts.chat_id:
        if continue_url:
            sys.exit("pass --chat-id or --continue-url, not both")
        # We need a site to resolve the chat_id -> URL. Use the first model.
        models_early = [m.strip() for m in opts.models.split(",") if m.strip()]
        if not models_early:
            sys.exit("--chat-id requires at least one --models site to resolve the URL")
        try:
            continue_url = webchat.chat_url(models_early[0], opts.chat_id)
        except Exception as exc:
            sys.exit(f"bad --chat-id {opts.chat_id!r}: {exc}")
    return continue_url


def _models_of(models_arg: str, *, single: bool) -> list[str]:
    """The known model sites asked; exactly one when continuing a conversation."""
    models = [m.strip() for m in models_arg.split(",") if m.strip()]
    unknown = [m for m in models if m not in webchat.SITES]
    if unknown:
        sys.exit(f"unknown model(s): {', '.join(unknown)}; known: {', '.join(webchat.SITES)}")
    if single and len(models) != 1:
        sys.exit(
            "--continue-url/--chat-id requires exactly one --models site (the conversation belongs to one site)"
        )
    return models


def _note_kept_tabs(results: Sequence[ConsoleRow]) -> None:
    # stdout is the JSON contract the agent parses; the kept-tab note (and
    # each resume url) lives in the result rows. Mirror a human breadcrumb to
    # stderr so it is visible in a terminal without disturbing that contract.
    kept = [r["url"] for r in results if "error" not in r and r["tab_kept"] and r["url"]]
    if kept:
        print(f"[console] left {len(kept)} tab(s) open: {', '.join(kept)}", file=sys.stderr)


def main(
    argv: Sequence[str],
    *,
    places: webchat.Places,
    pace: webchat.Pacing = webchat.STANDARD_PACE,
    clock: Callable[[], float],
) -> None:
    opts = options(_build_parser().parse_args(argv), _Options)
    continue_url = _continue_url(opts)

    prompt = _read_prompt(opts.prompt)
    if not prompt.strip():
        sys.exit("empty prompt")
    models = _models_of(opts.models, single=continue_url is not None)
    specs = [
        webchat.AskSpec(name=m, prompt=prompt, file=opts.file, conversation_url=continue_url)
        for m in models
    ]
    # Login check happens inside open_chat() (called by ask_many for fresh chats):
    # when no composer is found, it detects login walls and attempts auto-login
    # via "Continue with Google".  A login failure becomes an error row in the
    # result — one model's sign-in wall never sinks the others.
    rows = webchat.ask_many(
        specs, timeout=opts.timeout, keep_tab=opts.keep_tab, pace=pace, clock=clock
    )
    results = [_to_row(r) for r in rows]
    outdir = _save(prompt, results, places)

    if opts.keep_tab:
        _note_kept_tabs(results)

    print(
        json.dumps(
            {
                "prompt": prompt,
                "dir": str(outdir),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


# meiva:note
# The local `YYYYmmdd-HHMMSS` stamp a run's output directory is named by. It is read here because this is the entry -- the only place allowed to read the wall clock; every deadline inside the run counts on `time.monotonic`, handed to `main` as its `clock`.
def _run_stamp() -> str:
    return webchat.format_stamp(datetime.now().astimezone())


if __name__ == "__main__":
    main(
        sys.argv[1:],
        places=webchat.system_places(resolve_meiva_home(), user_home()),
        clock=time.monotonic,
    )
