# meiva:note
# Shared driver for the web-ai skill family: talk to a chat-style AI web app through the user's logged-in Chrome (the `chrome` MCP, reached as `meiva.mcps.chrome` against the shared headed Chrome the web-sources adapters use). The children (console, deep-research, media) do the same four things to a frontier-model web UI: open a fresh chat, type a prompt into the composer, submit it, and wait for the streamed answer to finish. That mechanic lives here once, so a selector fix happens in ONE place. This module is the shell: it takes data from the browser, hands it to the pure decision modules next to it (`_sites` profiles, `_js` script builders, `_pages` parsers, `_idle` completion and pacing, `_rows` result rows, `_landing` paths) and executes the answer. Every wait is a pause the shell owns; `Pacing` carries its lengths. Nothing here is imported into the agent namespace: the children import this module, call its functions and print JSON to stdout. Each navigation opens a fresh tab this driver owns, so concurrent callers never clobber each other's or the user's pages. Selectors are best known as of authoring and WILL drift; when a child raises "composer not found" or "answer empty", open the site in the shared browser, inspect the live DOM and update the selector list in `_sites.py`.
from __future__ import annotations

import contextlib
import time
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

# meiva:note
# The sibling modules are in this skill's own scripts/ dir, not an importable package. This module is import-only (no `__main__`), so it is always reached through a child sub-skill script (console, deep-research, media) whose own __file__-derived guard has already put this directory on `sys.path`. The children also reach `SITES`, `Places`, `assert_same_site`, `chat_url`, `download_by_click`, `downloads_root`, `now_stamp` and `system_places` through this module, so those are re-exported here (`X as X`).
from _dom import (
    click_by_text,
    resolve_clickable,
)
from _dom import download_by_click as download_by_click
from _idle import Answer, IdleState, Tab, fold_poll
from _js import (
    IMAGE_SOURCES_JS,
    LOCATION_JS,
    any_present_js,
    chip_visible_js,
    inject_prompt_js,
    read_answer_js,
    send_enabled_js,
    submit_js,
)
from _landing import Places as Places
from _pacing import STANDARD_PACE, Pacing, next_poll_delay
from _pages import (
    check_inject_result,
    is_login_wall,
    parse_evaluate_response,
    selected_page_id,
    submit_via,
)
from _rows import (
    UNIDENTIFIED_TAB_ERROR,
    AskRow,
    AskSpec,
    FailedRow,
    answer_row,
    failed_row,
    keeps_tab,
    tabs_left_open,
)
from _sites import SITES as SITES
from _sites import SiteProfile, site
from _sites import assert_same_site as assert_same_site
from _sites import chat_url as chat_url
from _utils import downloads_root as downloads_root
from _utils import now_stamp as now_stamp
from _utils import system_places as system_places

import meiva

# meiva:note
# The answer of an ask that did not wait: nothing yet, and not complete.
NOT_WAITED = Answer(text="", complete=False)


def _is_str_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_str_list(value: object) -> TypeGuard[list[str]]:
    return _is_object_list(value) and all(isinstance(item, str) for item in value)


# meiva:note
# Run JS in the shared browser's current page and return its parsed result. Raises if there is no fenced JSON block (the script threw, or no page is selected).
def evaluate(js: str) -> object:
    return parse_evaluate_response(meiva.mcps.chrome.evaluate_script(function=js))


# meiva:note
# Open `url` in a FRESH tab this driver owns, never reusing an existing page. new_page opens a new tab, selects it and loads `url` in one step, so every navigation lands on a tab created right here, never the page another caller (or the user) happens to have open; the inject, submit and read helpers then act on this just-selected tab. Returns the new tab's page id (for a later close_tab), or None if the page-list dump did not mark a selected tab.
def navigate(url: str) -> int | None:
    return selected_page_id(meiva.mcps.chrome.new_page(url=url))


# meiva:note
# Close the tab `navigate` opened, by its page id: the one-shot cleanup so a quick ask does not leave its tab piling up in the shared browser. Only ever closes the id handed in (a tab this driver created), so the user's own tabs are never touched. It no-ops on a None id and swallows the tool's own close refusal (the browser will not close the final tab, or the tab is already gone), because cleanup must not turn into a failure.
def close_tab(page_id: int | None) -> None:
    if page_id is None:
        return
    with contextlib.suppress(meiva.mcps.MCPCallError):
        meiva.mcps.chrome.close_page(pageId=page_id)


# meiva:note
# Make `page_id` the active tab so the read and inject helpers act on it. The shared browser has one active tab and `evaluate` runs on whichever it is, so a caller polling several owned tabs (`wait_many_idle`) selects each one right before reading it; that keeps concurrent conversations from reading each other's page.
def select(page_id: int) -> None:
    meiva.mcps.chrome.select_page(pageId=page_id)


# meiva:note
# The shared browser's current page URL (`location.href`).
def current_url() -> str:
    return str(evaluate(LOCATION_JS))


# meiva:note
# Close the tab `page_id` if the body raises, then re-raise: a setup step that fails after a tab was opened must not leave that tab piling up in the shared browser. close_tab no-ops on a None id.
@contextlib.contextmanager
def _closes_on_error(page_id: int | None) -> Generator[None]:
    try:
        yield
    except Exception:
        close_tab(page_id)
        raise


# meiva:note
# Poll a JS expression every `poll` seconds until it returns truthy, or until `timeout`. Returns whether it became truthy.
def wait_until(predicate_js: str, *, timeout: float, poll: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if evaluate(predicate_js):
            return True
        delay = next_poll_delay(now=time.monotonic(), deadline=deadline, poll=poll)
        if delay is None:
            return False
        time.sleep(delay)


def _composer_missing(prof: SiteProfile, name: str, where: str) -> RuntimeError:
    return RuntimeError(
        f"{prof.label}: composer not found at {where} "
        f"— not logged in, or the composer selector drifted (update SITES['{name}'].composer)"
    )


# meiva:note
# Wait for the composer to render on the current page. Raises if it never appears: not logged in (the site served a sign-in wall) or the composer selector drifted.
def _wait_composer(name: str, *, where: str, pace: Pacing) -> None:
    prof = site(name)
    if not wait_until(
        any_present_js(prof.composer), timeout=pace.composer_wait, poll=pace.ready_poll
    ):
        raise _composer_missing(prof, name, where)
    time.sleep(pace.editor_settle)


# meiva:note
# Walk one auto-login click path; False as soon as a step finds no control to click.
def _click_path(path: Sequence[str], pace: Pacing) -> bool:
    for step in path:
        if not click_by_text([step]):
            return False
        time.sleep(pace.login_click_gap)
    return True


# meiva:note
# Attempt the site's Google sign-in: each `auto_login` path is walked in turn and the first one after which the composer appears wins.
def _auto_login(prof: SiteProfile, pace: Pacing) -> bool:
    return any(
        _click_path(path, pace)
        and wait_until(any_present_js(prof.composer), timeout=pace.login_wait, poll=pace.ready_poll)
        for path in prof.auto_login
    )


# meiva:note
# Open the site's fresh-chat URL in a new owned tab and wait for the composer. When no composer appears in time, the page snapshot tells a sign-in wall (`login_indicators` text, then the `auto_login` click paths attempt a Google sign-in) from selector drift. Raises RuntimeError when no path reaches the composer, closing the tab it opened; any other failure after the tab opened closes it too. Returns the new tab's page id (for a later close_tab), or None if unmarked.
def open_chat(name: str, *, pace: Pacing = STANDARD_PACE) -> int | None:
    prof = site(name)
    page_id = navigate(prof.new_chat_url)
    with _closes_on_error(page_id):
        time.sleep(pace.page_settle)
        composer = any_present_js(prof.composer)
        if not wait_until(composer, timeout=pace.composer_wait, poll=pace.ready_poll):
            if not is_login_wall(page_snapshot(), prof.login_indicators):
                raise _composer_missing(prof, name, prof.new_chat_url)
            if not _auto_login(prof, pace):
                raise RuntimeError(
                    f"{prof.label}: not logged in. Please open {prof.new_chat_url} "
                    f"in the shared browser, log in (Continue with Google), then retry."
                )
        time.sleep(pace.editor_settle)
    return page_id


# meiva:note
# Type `text` into the composer, replacing whatever is there, and raise when nothing registered (the editor rejected the programmatic insert, or the selector drifted). The script is `_js.inject_prompt_js`.
def inject_prompt(name: str, text: str) -> None:
    prof = site(name)
    raw = evaluate(inject_prompt_js(prof.composer, text))
    check_inject_result(prof.label, raw if _is_str_dict(raw) else {})


# meiva:note
# Send the composed prompt: click the site's send button if present and enabled, otherwise dispatch an Enter keystroke on the composer. Returns a short tag of which path fired and raises if neither target exists.
def submit(name: str) -> str:
    prof = site(name)
    raw = evaluate(submit_js(prof.send, prof.composer))
    return submit_via(prof.label, raw if _is_str_dict(raw) else {})


# meiva:note
# Whether a known stop or streaming control is currently visible. A drifted stop selector matches nothing and returns False, the safe direction, since the idle detector then falls back to pure text stability.
def is_streaming(name: str) -> bool:
    return bool(evaluate(any_present_js(site(name).stop)))


# meiva:note
# Text of the LAST assistant message on the page, or '' if none yet.
def read_answer(name: str) -> str:
    return str(evaluate(read_answer_js(site(name).answer)))


# meiva:note
# One look at the currently selected tab folded into its idle state.
def _look(name: str, state: IdleState, pace: Pacing) -> IdleState:
    now = time.monotonic()
    text = read_answer(name)
    streaming = is_streaming(name)
    return fold_poll(state, text=text, streaming=streaming, now=now, stable_secs=pace.answer_stable)


# meiva:note
# Wait for the streamed answer to finish on the current page. Done means the last assistant message is non-empty, unchanged for `pace.answer_stable`, no stop control is visible and a real answer actually streamed in (see `_idle.fold_poll`). `complete` is False on timeout, with the best partial answer captured. The clock is time.monotonic, read once per look before the page is read, and the deadline is checked after the first look at the earliest.
def wait_until_idle(name: str, *, timeout: float, pace: Pacing = STANDARD_PACE) -> Answer:
    deadline = time.monotonic() + timeout
    state = IdleState()
    while True:
        state = _look(name, state, pace)
        if state.settled:
            break
        delay = next_poll_delay(now=time.monotonic(), deadline=deadline, poll=pace.answer_poll)
        if delay is None:
            break
        time.sleep(delay)
    return state.answer


# meiva:note
# Wait for several already-open tabs' answers to finish, polled round-robin. `tabs` maps a caller key to the owned tab. The shared browser has one active tab, so each tab is selected right before it is read; that lets independent conversations be polled together without clobbering. Loops until every tab settles or the shared `timeout` elapses, which overlaps the slow part (waiting for each streamed answer) instead of waiting them out one at a time. Returns key to the answer so far; `complete` is False for a tab that did not settle in time.
def wait_many_idle[K](
    tabs: Mapping[K, Tab], *, timeout: float, pace: Pacing = STANDARD_PACE
) -> dict[K, Answer]:
    if not tabs:
        return {}
    deadline = time.monotonic() + timeout
    states = {key: IdleState() for key in tabs}
    while True:
        for key, tab in tabs.items():
            if states[key].settled:
                continue
            select(tab.page_id)
            states[key] = _look(tab.site, states[key], pace)
        if all(state.settled for state in states.values()):
            break
        delay = next_poll_delay(now=time.monotonic(), deadline=deadline, poll=pace.answer_poll)
        if delay is None:
            break
        time.sleep(delay)
    return {key: state.answer for key, state in states.items()}


# meiva:note
# Open the conversation at `url` in a new owned tab and wait for its composer, closing the tab again when the composer never shows (a stale conversation, or a sign-in wall).
def _open_conversation(name: str, url: str, *, pace: Pacing) -> int | None:
    assert_same_site(url, name)
    page_id = navigate(url)
    with _closes_on_error(page_id):
        _wait_composer(name, where=url, pace=pace)
    return page_id


# meiva:note
# Open the tab, type the prompt, attach any file and submit it: the setup half shared by `ask` and `ask_many`. It opens a fresh chat (or re-opens `conversation_url` to continue it), injects `prompt`, attaches `file` after typing and submits. Returns the owned tab's page id (for a later select or close) and a short tag of how the submit fired. A failure after the tab opened (selector drift, attach timeout) closes it.
def _start(
    name: str,
    prompt: str,
    *,
    file: str | Path | None,
    conversation_url: str | None,
    pace: Pacing,
) -> tuple[int | None, str]:
    if conversation_url is not None:
        page_id = _open_conversation(name, conversation_url, pace=pace)
    else:
        page_id = open_chat(name, pace=pace)
    with _closes_on_error(page_id):
        inject_prompt(name, prompt)
        if file is not None:
            # meiva:note
            # Attach AFTER typing: with text present, the send control's disabled state reflects only the upload, which attach_file waits out.
            attach_file(name, file, pace=pace)
        via = submit(name)
    return page_id, via


# meiva:note
# Send `prompt` and return the finished answer. By default it opens a fresh chat; with `conversation_url` it continues that existing conversation instead (a follow-up turn). `file` attaches a local file through the site's own attach control. With wait=False it returns right after the submit with an empty answer, for messages whose response is collected later (answering a long job's clarifying question). Each ask opens its own tab and closes it once the answer is collected (one-shot, nothing piles up in the shared browser); the returned `url` is the durable handle to resume the conversation, passed back as `conversation_url`. `keep_tab` leaves the tab open instead, and wait=False always keeps it (the answer is still streaming). Any failure after the submit closes the tab as well, so an error never leaks it.
def ask(
    name: str,
    prompt: str,
    *,
    timeout: float = 180.0,
    file: str | Path | None = None,
    conversation_url: str | None = None,
    wait: bool = True,
    keep_tab: bool = False,
    pace: Pacing = STANDARD_PACE,
) -> AskRow:
    page_id, via = _start(name, prompt, file=file, conversation_url=conversation_url, pace=pace)
    keep = False
    try:
        answer = wait_until_idle(name, timeout=timeout, pace=pace) if wait else NOT_WAITED
        # meiva:note
        # Read the url BEFORE any close: closing moves the selection off this tab.
        url = current_url()
        keep = keeps_tab(wait=wait, keep_tab=keep_tab)
        return answer_row(name=name, via=via, answer=answer, url=url, tab_kept=keep)
    finally:
        if not keep:
            close_tab(page_id)


# meiva:note
# A panel member whose setup landed: the site asked, its owned tab and how the submit fired.
@dataclass(frozen=True)
class _Live:
    name: str
    page_id: int
    via: str


# meiva:note
# Ask several models concurrently over the shared browser; one row per spec, in spec order. Setup (open tab, inject, submit) runs serially per spec, which is fast, and each step opens or selects its OWN tab so the specs do not clobber one another. A spec whose setup raises becomes a failed row and is dropped from the wait set, so one dead model never sinks the panel. Every live tab is then polled round-robin until its answer settles or `timeout`, so the wall-clock is the slowest single answer, not the sum. Tabs are closed afterward unless `keep_tab`, including when the wait or the collection raised partway.
def ask_many(
    specs: Sequence[AskSpec],
    *,
    timeout: float = 180.0,
    keep_tab: bool = False,
    pace: Pacing = STANDARD_PACE,
) -> list[AskRow | FailedRow]:
    rows: dict[int, AskRow | FailedRow] = {}
    live: dict[int, _Live] = {}
    for i, spec in enumerate(specs):
        try:
            page_id, via = _start(
                spec.name,
                spec.prompt,
                file=spec.file,
                conversation_url=spec.conversation_url,
                pace=pace,
            )
        except Exception as exc:
            rows[i] = failed_row(spec.name, f"{type(exc).__name__}: {exc}")
            continue
        if page_id is None:
            rows[i] = failed_row(spec.name, UNIDENTIFIED_TAB_ERROR)
            continue
        live[i] = _Live(spec.name, page_id, via)

    closed: set[int] = set()
    try:
        waited = wait_many_idle(
            {i: Tab(v.name, v.page_id) for i, v in live.items()},
            timeout=timeout,
            pace=pace,
        )
        for i, v in live.items():
            select(v.page_id)
            url = current_url()
            if not keep_tab:
                close_tab(v.page_id)
                closed.add(v.page_id)
            rows[i] = answer_row(
                name=v.name, via=v.via, answer=waited[i], url=url, tab_kept=keep_tab
            )
    finally:
        for page_id in tabs_left_open(
            [v.page_id for v in live.values()], closed, keep_tab=keep_tab
        ):
            close_tab(page_id)
    return [rows[i] for i in range(len(specs))]


# meiva:note
# The rendered accessibility tree of the current page, as text. It crosses frame boundaries the page DOM cannot (a widget rendered in a sandboxed frame still shows up here), so it doubles as the read path for content that CSS selectors cannot reach.
def page_snapshot() -> str:
    return meiva.mcps.chrome.take_snapshot()


# meiva:note
# Attach a local file to the message being composed, via the site's own attach control. It walks `attach_path` (a click sequence whose FINAL control opens the OS file chooser, intercepted and fed `path`), then waits for the upload to finish by polling the send control's disabled state. Call it after `inject_prompt`: with an empty composer the send control is disabled anyway and the upload wait could never tell the two apart. Raises if the file is missing, an attach control cannot be found, or the upload does not finish within `pace.upload_wait`.
def attach_file(name: str, path: str | Path, *, pace: Pacing = STANDARD_PACE) -> None:
    prof = site(name)
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"attachment not found: {p}")
    steps = prof.attach_path
    for step in steps[:-1]:
        if not click_by_text(step):
            raise RuntimeError(
                f"{prof.label}: could not find {step!r} while attaching "
                f"(update SITES['{name}'].attach_path)"
            )
        time.sleep(pace.attach_click_gap)
    hit = resolve_clickable(steps[-1])
    if hit is None:
        raise RuntimeError(
            f"{prof.label}: could not find {steps[-1]!r} while attaching "
            f"(update SITES['{name}'].attach_path)"
        )
    # meiva:note
    # The img-src baseline is taken BEFORE the upload: an image attachment registers as a NEW thumbnail, which the chip probe below recognizes against it.
    raw_baseline = evaluate(IMAGE_SOURCES_JS)
    baseline: list[str] = raw_baseline if _is_str_list(raw_baseline) else []
    meiva.mcps.chrome.upload_file(uid=hit.uid, filePath=str(p))
    # meiva:note
    # The chip proves the file registered. Some sites keep send enabled on text alone, so the send-state probe alone would pass before the file landed; the send probe after this covers the sites that DO disable it mid-upload.
    chip = chip_visible_js(p.name, baseline)
    if not wait_until(chip, timeout=pace.upload_wait, poll=pace.upload_poll):
        raise RuntimeError(
            f"{prof.label}: attachment {p.name!r} never appeared on the page "
            f"within {pace.upload_wait}s — the file chooser was not intercepted, or "
            f"the upload failed"
        )
    if not wait_until(send_enabled_js(prof.send), timeout=pace.upload_wait, poll=pace.upload_poll):
        raise RuntimeError(
            f"{prof.label}: attachment upload did not finish within {pace.upload_wait}s "
            f"(send control still disabled)"
        )


# meiva:note
# Whether the site is logged in in the shared browser: a fresh tab opens on the site and the composer is awaited for `pace.login_probe_wait`. A composer means logged in; without one the answer is False whether the page shows a sign-in wall or nothing recognizable, and the caller's next step (`open_chat`) tells them apart. The tab is closed before returning, so this is a non-invasive probe.
def check_login(name: str, *, pace: Pacing = STANDARD_PACE) -> bool:
    prof = site(name)
    page_id = navigate(prof.new_chat_url)
    try:
        return wait_until(
            any_present_js(prof.composer), timeout=pace.login_probe_wait, poll=pace.ready_poll
        )
    finally:
        close_tab(page_id)
