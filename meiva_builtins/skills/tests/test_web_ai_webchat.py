# meiva:note
# Tests of the shell of the web-ai shared driver (meiva_builtins/skills/integrations/web-ai/scripts/webchat.py), run against a stand-in browser behind a real MCP daemon socket (`web_ai/chrome_stand_in.py`). Every navigation must open its OWN fresh tab, a finished ask closes its tab, and a failure anywhere after a tab opened closes it too. The decisions the shell executes are pure modules, tested with plain values in `web_ai/test_web_ai_decisions.py`.
from __future__ import annotations

import re
from pathlib import Path

import pytest

import meiva
from base.host.tests.short_tmp import short_tmp as short_tmp
from meiva.mcps.tests.stub_daemon import stub_daemon as stub_daemon
from meiva_builtins.skills.tests.web_ai.chrome_stand_in import (
    PageScript,
    ServeChrome,
    StandInChrome,
)
from meiva_builtins.skills.tests.web_ai.chrome_stand_in import serve_chrome as serve_chrome
from meiva_builtins.skills.tests.web_ai.driver_modules import load_driver_modules

M = load_driver_modules()
webchat = M.webchat

CHATGPT = "https://chatgpt.com/"
GEMINI = "https://gemini.google.com/app"
CLAUDE = "https://claude.ai/new"
CONVERSATION = "https://chatgpt.com/c/abc"

# meiva:note
# Waits that take no time: the stand-in browser answers at once, so only the deadlines (a fraction of a second) are ever waited out.
FAST = M.pacing.Pacing(
    page_settle=0.0,
    composer_wait=0.3,
    editor_settle=0.0,
    login_click_gap=0.0,
    login_wait=0.3,
    login_probe_wait=0.3,
    ready_poll=0.01,
    answer_stable=0.0,
    answer_poll=0.0,
    attach_click_gap=0.0,
    upload_wait=0.3,
    upload_poll=0.01,
    download_poll=0.01,
    download_recheck=0.01,
)


# meiva:note
# A page that streams two texts with the stop control up, then holds the final one with the control gone, and lands on a conversation URL once the prompt is submitted.
def answering(final: str = "final answer", url: str = CONVERSATION) -> PageScript:
    return PageScript(after_submit_url=url, answers=("partial", final), stops=(True, False))


def browser(
    serve: ServeChrome, pages: dict[str, PageScript] | None = None, *, marks_selected: bool = True
) -> StandInChrome:
    return serve(
        StandInChrome(js=M.js, profiles=M.sites.SITES, pages=pages, marks_selected=marks_selected)
    )


def test_downloads_root_creates_the_capability_directory(tmp_path: Path) -> None:
    places = M.landing.Places(home=tmp_path / ".meiva-t1", downloads=tmp_path / "Downloads")
    root = M.utils.downloads_root("console", places=places)
    assert root == tmp_path / "Downloads" / "meiva_meiva-t1_web-ai" / "console"
    assert root.is_dir()
    assert M.utils.downloads_root("console", places=places) == root


def test_system_places_reads_the_home_and_the_downloads_folder(unit_home: Path) -> None:
    places = M.utils.system_places()
    assert places.home.resolve() == unit_home.resolve()
    assert places.downloads == Path("~/Downloads").expanduser()


def test_now_stamp_is_a_sortable_local_time_stamp() -> None:
    assert re.fullmatch(r"\d{8}-\d{6}", M.utils.now_stamp())


def test_navigate_opens_new_tab_not_navigate_page(serve_chrome: ServeChrome) -> None:
    chrome = browser(serve_chrome)

    page_id = webchat.navigate(CHATGPT)

    assert page_id == 2
    assert chrome.events == [f"new_page {CHATGPT}"]
    assert chrome.tabs[1].location == "https://user.example/"


def test_navigate_returns_none_when_the_listing_marks_no_selected_tab(
    serve_chrome: ServeChrome,
) -> None:
    browser(serve_chrome, marks_selected=False)
    assert webchat.navigate(CHATGPT) is None


def test_select_activates_tab(serve_chrome: ServeChrome) -> None:
    chrome = browser(serve_chrome)
    webchat.navigate(CHATGPT)
    webchat.navigate(GEMINI)
    assert chrome.selected == 3

    webchat.select(2)

    assert chrome.selected == 2


def test_open_chat_returns_owned_page_id(serve_chrome: ServeChrome) -> None:
    chrome = browser(serve_chrome)

    assert webchat.open_chat("chatgpt", pace=FAST) == 2

    assert sorted(chrome.tabs) == [1, 2]
    assert chrome.tabs[2].location == webchat.site("chatgpt").new_chat_url


def test_open_chat_signs_in_through_google_when_the_page_shows_a_wall(
    serve_chrome: ServeChrome,
) -> None:
    wall = PageScript(
        composer=False,
        snapshot='Log in to ChatGPT\nuid=1_1 button "Continue with Google"',
        reveals_composer=("1_1",),
    )
    chrome = browser(serve_chrome, {CHATGPT: wall})

    assert webchat.open_chat("chatgpt", pace=FAST) == 2

    assert chrome.tabs[2].composer
    assert "click 2 1_1" in chrome.events


def test_open_chat_closes_its_tab_when_no_sign_in_path_works(serve_chrome: ServeChrome) -> None:
    chrome = browser(
        serve_chrome, {CHATGPT: PageScript(composer=False, snapshot="Log in to ChatGPT")}
    )

    with pytest.raises(RuntimeError, match="ChatGPT: not logged in"):
        webchat.open_chat("chatgpt", pace=FAST)

    assert sorted(chrome.tabs) == [1]


def test_open_chat_reports_selector_drift_and_closes_its_tab(serve_chrome: ServeChrome) -> None:
    chrome = browser(serve_chrome, {CHATGPT: PageScript(composer=False, snapshot="nothing known")})

    with pytest.raises(RuntimeError, match=r"composer not found at https://chatgpt.com/"):
        webchat.open_chat("chatgpt", pace=FAST)

    assert sorted(chrome.tabs) == [1]


# meiva:note
# A browser that answers no script (no site profile for the page): the first composer probe is refused, and the tab opened a moment earlier must not leak.
def test_open_chat_closes_its_tab_when_the_browser_fails_mid_wait(
    serve_chrome: ServeChrome,
) -> None:
    chrome = serve_chrome(StandInChrome(js=M.js, profiles={}))

    with pytest.raises(meiva.mcps.MCPCallError, match="no site profile"):
        webchat.open_chat("chatgpt", pace=FAST)

    assert sorted(chrome.tabs) == [1]


def test_close_tab_closes_by_id(serve_chrome: ServeChrome) -> None:
    chrome = browser(serve_chrome)
    page_id = webchat.navigate(CHATGPT)

    webchat.close_tab(page_id)

    assert sorted(chrome.tabs) == [1]


def test_close_tab_none_is_noop(serve_chrome: ServeChrome) -> None:
    chrome = browser(serve_chrome)
    webchat.close_tab(None)
    assert chrome.events == []


# meiva:note
# The browser refuses to close the final tab; cleanup must not raise.
def test_close_tab_swallows_close_error(
    serve_chrome: ServeChrome,
) -> None:
    chrome = browser(serve_chrome)

    webchat.close_tab(1)

    assert sorted(chrome.tabs) == [1]
    assert chrome.events == ["close_page 1"]


# meiva:note
# Only the tool's own refusal is cleanup noise; a browser that cannot be reached is a failure.
def test_close_tab_does_not_hide_a_bug(serve_chrome: ServeChrome) -> None:
    chrome = browser(serve_chrome)
    assert chrome.socket is not None
    chrome.socket.unlink()

    with pytest.raises(meiva.mcps.MCPConnectError, match="MCP daemon unavailable"):
        webchat.close_tab(2)


def test_inject_prompt_types_into_the_composer_and_submit_sends_it(
    serve_chrome: ServeChrome,
) -> None:
    chrome = browser(serve_chrome)
    webchat.navigate(CHATGPT)

    webchat.inject_prompt("chatgpt", 'say "hi"\nthen `code`')
    via = webchat.submit("chatgpt")

    assert chrome.tabs[2].typed == 'say "hi"\nthen `code`'
    assert chrome.tabs[2].submitted and via == "button:send"


def test_inject_prompt_raises_when_the_editor_rejects_the_insert(serve_chrome: ServeChrome) -> None:
    browser(serve_chrome, {CHATGPT: PageScript(registers=False)})
    webchat.navigate(CHATGPT)

    with pytest.raises(RuntimeError, match="prompt did not register in the composer"):
        webchat.inject_prompt("chatgpt", "hello")


def test_submit_falls_back_to_enter_and_raises_without_any_target(
    serve_chrome: ServeChrome,
) -> None:
    browser(serve_chrome, {CHATGPT: PageScript(send="enter"), GEMINI: PageScript(send="none")})
    webchat.navigate(CHATGPT)
    assert webchat.submit("chatgpt") == "enter"

    webchat.navigate(GEMINI)
    with pytest.raises(RuntimeError, match=r"Gemini: could not submit \(no-send-target\)"):
        webchat.submit("gemini")


# meiva:note
# The tab open_chat owned is closed once the answer is collected.
def test_ask_one_shot_closes_owned_tab(serve_chrome: ServeChrome) -> None:
    chrome = browser(serve_chrome, {CHATGPT: answering()})

    r = webchat.ask("chatgpt", "hello", timeout=5.0, pace=FAST)

    assert r["tab_kept"] is False
    assert sorted(chrome.tabs) == [1]
    assert chrome.events.count("close_page 2") == 1
    assert (r["site"], r["label"], r["submitted_via"]) == ("chatgpt", "ChatGPT", "button:send")
    assert r["answer"] == "final answer" and r["complete"] is True
    assert r["url"] == CONVERSATION and r["chat_id"] == "abc"


def test_ask_types_the_prompt_into_the_fresh_chat(serve_chrome: ServeChrome) -> None:
    chrome = browser(serve_chrome, {CHATGPT: answering()})
    webchat.ask("chatgpt", "hello", keep_tab=True, timeout=5.0, pace=FAST)
    assert chrome.tabs[2].typed == "hello"


def test_ask_keep_tab_leaves_tab_open(serve_chrome: ServeChrome) -> None:
    chrome = browser(serve_chrome, {CHATGPT: answering()})

    r = webchat.ask("chatgpt", "hello", keep_tab=True, timeout=5.0, pace=FAST)

    assert sorted(chrome.tabs) == [1, 2]
    assert r["tab_kept"] is True


# meiva:note
# The answer is still streaming and is collected later through the conversation URL, so the tab stays.
def test_ask_wait_false_always_keeps_tab(serve_chrome: ServeChrome) -> None:
    chrome = browser(serve_chrome, {CHATGPT: answering()})

    r = webchat.ask("chatgpt", "hello", wait=False, pace=FAST)

    assert sorted(chrome.tabs) == [1, 2]
    assert r["tab_kept"] is True and r["answer"] == "" and r["complete"] is False
    assert r["url"] == CONVERSATION


def test_ask_returns_the_partial_answer_when_the_wait_times_out(serve_chrome: ServeChrome) -> None:
    streaming = PageScript(after_submit_url=CONVERSATION, answers=("still going",), stops=(True,))
    chrome = browser(serve_chrome, {CHATGPT: streaming})

    r = webchat.ask("chatgpt", "hello", timeout=0.1, pace=FAST)

    assert r["answer"] == "still going" and r["complete"] is False
    assert sorted(chrome.tabs) == [1]


# meiva:note
# The first page opened is the conversation, not a fresh chat, and its own tab is closed afterwards.
def test_ask_continue_url_closes_its_own_tab(serve_chrome: ServeChrome) -> None:
    chrome = browser(serve_chrome, {CHATGPT: PageScript(), CONVERSATION: answering()})

    r = webchat.ask("chatgpt", "follow up", conversation_url=CONVERSATION, timeout=5.0, pace=FAST)

    assert chrome.events[0] == f"new_page {CONVERSATION}"
    assert sorted(chrome.tabs) == [1]
    assert r["chat_id"] == "abc"


def test_ask_refuses_a_conversation_url_on_another_host_before_opening_a_tab(
    serve_chrome: ServeChrome,
) -> None:
    chrome = browser(serve_chrome)

    with pytest.raises(ValueError, match=r"refusing to navigate to non-chatgpt\.com URL"):
        webchat.ask("chatgpt", "hi", conversation_url="https://evil.example/c/abc", pace=FAST)

    assert chrome.events == []


def test_ask_closes_the_tab_when_the_conversation_has_no_composer(
    serve_chrome: ServeChrome,
) -> None:
    chrome = browser(serve_chrome, {CONVERSATION: PageScript(composer=False)})

    with pytest.raises(RuntimeError, match=r"composer not found at https://chatgpt\.com/c/abc"):
        webchat.ask("chatgpt", "hi", conversation_url=CONVERSATION, pace=FAST)

    assert sorted(chrome.tabs) == [1]


# meiva:note
# An error during the answer wait must still close the owned tab, never leak it into the shared browser.
def test_ask_closes_owned_tab_when_wait_raises(serve_chrome: ServeChrome) -> None:
    chrome = browser(serve_chrome, {CHATGPT: PageScript(answer_fails=True)})

    with pytest.raises(meiva.mcps.MCPCallError, match="stopped answering"):
        webchat.ask("chatgpt", "hello", timeout=5.0, pace=FAST)

    assert sorted(chrome.tabs) == [1]


# meiva:note
# With no URL to come back to, a tab kept for a later read is a tab nobody can reach.
def test_ask_wait_false_closes_the_tab_when_the_url_cannot_be_read(
    serve_chrome: ServeChrome,
) -> None:
    chrome = browser(serve_chrome, {CHATGPT: PageScript(location_fails=True)})

    with pytest.raises(meiva.mcps.MCPCallError, match="the tab is gone"):
        webchat.ask("chatgpt", "hello", wait=False, pace=FAST)

    assert sorted(chrome.tabs) == [1]


# meiva:note
# The setup opened the tab; if a later step (submit) raises, the tab is closed before the error propagates, not left piling up.
def test_start_closes_tab_when_submit_raises(serve_chrome: ServeChrome) -> None:
    chrome = browser(serve_chrome, {CHATGPT: PageScript(send="none")})

    with pytest.raises(RuntimeError, match="could not submit"):
        webchat.ask("chatgpt", "hello", pace=FAST)

    assert sorted(chrome.tabs) == [1]


def test_wait_until_idle_settles_on_the_final_answer(serve_chrome: ServeChrome) -> None:
    browser(serve_chrome, {CHATGPT: answering()})
    webchat.navigate(CHATGPT)
    webchat.submit("chatgpt")

    answer = webchat.wait_until_idle("chatgpt", timeout=5.0, pace=FAST)

    assert (answer.text, answer.complete) == ("final answer", True)


def test_wait_many_idle_empty_is_noop(serve_chrome: ServeChrome) -> None:
    chrome = browser(serve_chrome)
    assert webchat.wait_many_idle({}, timeout=1.0, pace=FAST) == {}
    assert chrome.events == []


# meiva:note
# Reads resolve against the CURRENTLY-selected tab, so correct answers per key prove each tab is selected before it is read (otherwise the two answers would cross).
def test_wait_many_idle_polls_each_tab_under_its_own_selection(serve_chrome: ServeChrome) -> None:
    chatgpt = PageScript(answers=("ans-chatgpt",), stops=(True, False))
    gemini = PageScript(answers=("ans-gemini",), stops=(True, False))
    browser(serve_chrome, {CHATGPT: chatgpt, GEMINI: gemini})
    first = webchat.navigate(CHATGPT)
    webchat.submit("chatgpt")
    second = webchat.navigate(GEMINI)
    webchat.submit("gemini")
    assert first is not None and second is not None

    out = webchat.wait_many_idle(
        {"A": M.idle.Tab("chatgpt", first), "B": M.idle.Tab("gemini", second)},
        timeout=5.0,
        pace=FAST,
    )

    assert (out["A"].text, out["A"].complete) == ("ans-chatgpt", True)
    assert (out["B"].text, out["B"].complete) == ("ans-gemini", True)


# meiva:note
# A tab that never stops streaming never settles and times out with the partial text.
def test_wait_many_idle_returns_partial_on_timeout(serve_chrome: ServeChrome) -> None:
    browser(serve_chrome, {CHATGPT: PageScript(answers=("still going",), stops=(True,))})
    page_id = webchat.navigate(CHATGPT)
    webchat.submit("chatgpt")
    assert page_id is not None

    out = webchat.wait_many_idle({"A": M.idle.Tab("chatgpt", page_id)}, timeout=-1.0, pace=FAST)

    assert (out["A"].text, out["A"].complete) == ("still going", False)


def _spec(name: str) -> object:
    return webchat.AskSpec(name=name, prompt="q")


# meiva:note
# The slow part (reading answers) starts only AFTER both tabs are submitted: that is the overlap.
def test_ask_many_sets_up_all_tabs_before_waiting(serve_chrome: ServeChrome) -> None:
    chrome = browser(
        serve_chrome,
        {CHATGPT: answering("A-chatgpt", "https://chatgpt.com/c/1"), GEMINI: answering("A-gemini")},
    )

    rows = webchat.ask_many([_spec("chatgpt"), _spec("gemini")], timeout=10.0, pace=FAST)

    submits = [i for i, e in enumerate(chrome.events) if e.startswith("submit ")]
    reads = [i for i, e in enumerate(chrome.events) if e.startswith("answer ")]
    assert len(submits) == 2 and min(reads) > max(submits)
    assert [r["site"] for r in rows] == ["chatgpt", "gemini"]
    assert rows[0]["answer"] == "A-chatgpt" and rows[1]["answer"] == "A-gemini"
    assert sorted(chrome.tabs) == [1]


# meiva:note
# The live models still answered: one dead model did not sink the panel.
def test_ask_many_setup_failure_isolated_to_its_row(serve_chrome: ServeChrome) -> None:
    chrome = browser(
        serve_chrome,
        {
            CHATGPT: answering("A-chatgpt"),
            GEMINI: PageScript(composer=False, snapshot="nothing known"),
            CLAUDE: answering("A-claude", "https://claude.ai/chat/c1"),
        },
    )

    rows = webchat.ask_many(
        [_spec("chatgpt"), _spec("gemini"), _spec("claude")], timeout=10.0, pace=FAST
    )

    assert [r["site"] for r in rows] == ["chatgpt", "gemini", "claude"]
    assert "composer not found" in rows[1].get("error", "")
    assert rows[0]["answer"] == "A-chatgpt" and "error" not in rows[0]
    assert rows[2]["answer"] == "A-claude" and "error" not in rows[2]
    assert sorted(chrome.tabs) == [1]


def test_ask_many_none_page_id_becomes_error_row(serve_chrome: ServeChrome) -> None:
    browser(serve_chrome, {CHATGPT: answering()}, marks_selected=False)

    rows = webchat.ask_many([_spec("chatgpt")], timeout=10.0, pace=FAST)

    assert "page-list format drift" in rows[0].get("error", "")


def test_ask_many_unknown_site_raises_before_any_tab_opens(serve_chrome: ServeChrome) -> None:
    chrome = browser(serve_chrome)

    with pytest.raises(ValueError, match="unknown site 'bard'"):
        webchat.ask_many([_spec("bard")], timeout=10.0, pace=FAST)

    assert chrome.events == []


def test_ask_many_keep_tab_leaves_tabs_open(serve_chrome: ServeChrome) -> None:
    chrome = browser(serve_chrome, {CHATGPT: answering()})

    rows = webchat.ask_many([_spec("chatgpt")], timeout=10.0, keep_tab=True, pace=FAST)

    assert sorted(chrome.tabs) == [1, 2]
    assert rows[0].get("tab_kept") is True


# meiva:note
# If the shared wait raises after all tabs are set up, every live tab is still closed, not leaked.
def test_ask_many_closes_live_tabs_when_wait_raises(serve_chrome: ServeChrome) -> None:
    chrome = browser(serve_chrome, {CHATGPT: answering(), GEMINI: PageScript(answer_fails=True)})

    with pytest.raises(meiva.mcps.MCPCallError, match="stopped answering"):
        webchat.ask_many([_spec("chatgpt"), _spec("gemini")], timeout=10.0, pace=FAST)

    assert sorted(chrome.tabs) == [1]
    assert sorted(e for e in chrome.events if e.startswith("close_page")) == [
        "close_page 2",
        "close_page 3",
    ]


# meiva:note
# A raise partway through the collect loop (after the first tab already closed) still closes the remaining tab, and never double-closes the first: page 2 closes in the loop, page 3 in the finally, each exactly once.
def test_ask_many_mid_loop_raise_closes_rest_exactly_once(serve_chrome: ServeChrome) -> None:
    chrome = browser(
        serve_chrome,
        {
            CHATGPT: answering(),
            GEMINI: PageScript(location_fails=True, answers=("x",), stops=(True, False)),
        },
    )

    with pytest.raises(meiva.mcps.MCPCallError, match="the tab is gone"):
        webchat.ask_many([_spec("chatgpt"), _spec("gemini")], timeout=10.0, pace=FAST)

    closes = [e for e in chrome.events if e.startswith("close_page")]
    assert sorted(closes) == ["close_page 2", "close_page 3"]
    assert sorted(chrome.tabs) == [1]


def test_check_login_detects_login_indicators(serve_chrome: ServeChrome) -> None:
    chrome = browser(
        serve_chrome, {CHATGPT: PageScript(composer=False, snapshot="Log in to ChatGPT")}
    )

    assert webchat.check_login("chatgpt", pace=FAST) is False

    assert sorted(chrome.tabs) == [1]


def test_check_login_confirms_logged_in_when_composer_present(serve_chrome: ServeChrome) -> None:
    chrome = browser(serve_chrome)

    assert webchat.check_login("chatgpt", pace=FAST) is True

    assert sorted(chrome.tabs) == [1]


_ATTACH_MENU = 'uid=a1 button "Add files and more"\nuid=a2 menuitem "Add photos & files"'


# meiva:note
# The menu control is clicked first, then the file chooser control is handed the file.
def test_attach_file_walks_the_attach_path_and_hands_over_the_file(
    serve_chrome: ServeChrome, tmp_path: Path
) -> None:
    note = tmp_path / "note.txt"
    note.write_text("hello", encoding="utf-8")
    chrome = browser(serve_chrome, {CHATGPT: PageScript(snapshot=_ATTACH_MENU)})
    webchat.navigate(CHATGPT)

    webchat.attach_file("chatgpt", note, pace=FAST)

    assert chrome.tabs[2].uploads == ["note.txt"]
    assert "click 2 a1" in chrome.events


def test_attach_file_raises_for_a_missing_file_without_touching_the_browser(
    serve_chrome: ServeChrome, tmp_path: Path
) -> None:
    chrome = browser(serve_chrome)

    with pytest.raises(FileNotFoundError, match="attachment not found"):
        webchat.attach_file("chatgpt", tmp_path / "absent.txt", pace=FAST)

    assert chrome.events == []


def test_attach_file_names_the_control_it_could_not_find(
    serve_chrome: ServeChrome, tmp_path: Path
) -> None:
    note = tmp_path / "note.txt"
    note.write_text("hello", encoding="utf-8")
    browser(serve_chrome, {CHATGPT: PageScript(snapshot="")})
    webchat.navigate(CHATGPT)

    with pytest.raises(RuntimeError, match=r"could not find \('Add files and more',\)"):
        webchat.attach_file("chatgpt", note, pace=FAST)


def _export_page(downloads: Path, *, saves: bool = True) -> PageScript:
    saved = (("d1", downloads / "report.md"),) if saves else ()
    return PageScript(snapshot='uid=d1 menuitem "Export to Markdown"', saves_on_click=saved)


def test_download_by_click_moves_the_new_file_to_dest(
    serve_chrome: ServeChrome, tmp_path: Path
) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "older.md").write_text("old", encoding="utf-8")
    browser(serve_chrome, {CHATGPT: _export_page(downloads)})
    webchat.navigate(CHATGPT)
    dest = tmp_path / "out" / "report.md"

    got = webchat.download_by_click(
        ["Export to Markdown"],
        dest=dest,
        suffixes=(".md",),
        downloads=downloads,
        timeout=2.0,
        pace=FAST,
    )

    assert got == dest and dest.read_text(encoding="utf-8") == "downloaded"
    assert sorted(p.name for p in downloads.iterdir()) == ["older.md"]


def test_download_by_click_is_none_when_the_control_is_missing(
    serve_chrome: ServeChrome, tmp_path: Path
) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    browser(serve_chrome, {CHATGPT: PageScript(snapshot="")})
    webchat.navigate(CHATGPT)

    got = webchat.download_by_click(
        ["Export to Markdown"],
        dest=tmp_path / "report.md",
        suffixes=(".md",),
        downloads=downloads,
        pace=FAST,
    )

    assert got is None


def test_download_by_click_is_none_when_no_new_file_lands_in_time(
    serve_chrome: ServeChrome, tmp_path: Path
) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "report.md").write_text("already there", encoding="utf-8")
    browser(serve_chrome, {CHATGPT: _export_page(downloads, saves=False)})
    webchat.navigate(CHATGPT)

    got = webchat.download_by_click(
        ["Export to Markdown"],
        dest=tmp_path / "out.md",
        suffixes=(".md",),
        downloads=downloads,
        timeout=0.1,
        pace=FAST,
    )

    assert got is None
    assert (downloads / "report.md").read_text(encoding="utf-8") == "already there"
