# meiva:note
# The text a developer reads when a hint is open. A hint id prints its full text (why,
# instead, see, how to answer) at its first site; later sites of the same id print only
# the site line, and a run caps how many sites of one id it lists so one noisy id cannot
# bury the others.
from __future__ import annotations

import textwrap
from collections.abc import Iterable

from scripts.lint.diagnostics.diff_hints.acks import suggestions
from scripts.lint.diagnostics.diff_hints.catalog import spec_of
from scripts.lint.diagnostics.diff_hints.model import Ack, HintId, Site

_WIDTH = 100
_LABEL = 9
_SITES_SHOWN = 8


def _site_line(site: Site) -> str:
    return f"{site.path}:{site.line}: hint {site.hint.value}: {site.detail}"


def _field(label: str, text: str) -> str:
    head = f"  {label + ':':<{_LABEL}}"
    return textwrap.fill(
        text,
        width=_WIDTH,
        initial_indent=head,
        subsequent_indent=" " * len(head),
        break_long_words=False,
        break_on_hyphens=False,
    )


def _full_text(site: Site) -> list[str]:
    spec = spec_of(site.hint)
    trailer = f"Hint-Ack: {site.hint.value} {site.path}:{site.line} -- <reason>"
    pad = " " * (len("  ") + _LABEL)
    return [
        _site_line(site),
        _field("why", spec.why),
        _field("instead", spec.instead),
        _field("see", spec.see),
        f"  {'answer:':<{_LABEL}}change the code, or add to the pull request body:",
        f"{pad}{trailer}",
    ]


def _by_hint(sites: Iterable[Site]) -> dict[HintId, list[Site]]:
    grouped: dict[HintId, list[Site]] = {}
    for site in sites:
        grouped.setdefault(site.hint, []).append(site)
    return grouped


def _hint_block(members: list[Site], limit: int | None) -> list[str]:
    shown = members if limit is None else members[:limit]
    lines = _full_text(shown[0])
    lines.extend(_site_line(site) for site in shown[1:])
    hidden = members[len(shown) :]
    if hidden:
        files = len({site.path for site in hidden})
        lines.append(f"... {len(hidden)} more `{members[0].hint.value}` site(s) in {files} file(s)")
    return lines


# meiva:note
# The report for the open sites. `limit` caps the sites listed per hint id (None lists
# all); the trailer templates at the end always cover every open site.
def render_open(sites: Iterable[Site], *, limit: int | None = _SITES_SHOWN) -> str:
    open_sites = list(sites)
    lines: list[str] = []
    for members in _by_hint(open_sites).values():
        lines.extend(_hint_block(members, limit))
        lines.append("")
    lines.append(
        f"diff hints: {len(open_sites)} site(s) need an answer. Fix the code, or answer in the "
        "pull request body -- the copy a squash merge keeps -- or in a commit message (a path "
        "without a line answers every site of that id in the file):"
    )
    lines.extend(f"  {template}" for template in suggestions(open_sites))
    return "\n".join(lines)


_BROAD_SITES = 10


def _scope(ack: Ack) -> str:
    return ack.path if ack.line is None else f"{ack.path}:{ack.line}"


def _is_broad(ack: Ack, covers: int) -> bool:
    return ack.path.endswith("/") or covers >= _BROAD_SITES


# meiva:note
# The answers of a branch as a table for a reviewer: which commit gave it, what it covers, how
# many sites it answers and the reason. A directory trailer, or one that answers ten sites or
# more, is marked broad: the reason has to carry the weight there.
def render_summary(entries: Iterable[tuple[str, Ack, int]], open_sites: int) -> str:
    rows = list(entries)
    lines = [
        "## Diff hint answers",
        "",
        f"{len(rows)} answer(s) on this branch; {open_sites} site(s) still open.",
    ]
    if rows:
        lines += ["", "| commit | hint | scope | covers | reason |", "|---|---|---|---|---|"]
        for sha, ack, covers in rows:
            mark = " **broad**" if _is_broad(ack, covers) else ""
            reason = ack.reason.replace("|", "\\|")
            lines.append(
                f"| `{sha}` | `{ack.hint.value}` | `{_scope(ack)}`{mark} | {covers} | {reason} |"
            )
    return "\n".join(lines)


def render_errors(errors: Iterable[str]) -> str:
    return "\n".join(f"diff hints: {error}" for error in errors)
