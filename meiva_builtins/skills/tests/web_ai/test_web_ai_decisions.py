# meiva:note
# Tests of the decisions of the web-ai shared driver (meiva_builtins/skills/integrations/web-ai/scripts): each pure module is called with values and its return value is asserted. They cover the site profiles and URLs, the script builders, the parsers of what the chrome MCP returns, the idle detector and its next delay, the result rows and the landing paths. The shell that executes them is tested against a stand-in browser in test_web_ai_webchat.py.
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

import pytest

from meiva_builtins.skills.tests.web_ai.driver_modules import load_driver_modules

M = load_driver_modules()

_PAGE_LIST = (
    "## Pages\n"
    "1: http://localhost:3000/?agent_id=240\n"
    "2: https://chatgpt.com/c/abc [selected]\n"
    "3: https://drive.google.com/\n"
)

CHATGPT = "https://chatgpt.com/"
CONVERSATION = "https://chatgpt.com/c/abc"


def test_selected_page_id_reads_marked_line() -> None:
    assert M.pages.selected_page_id(_PAGE_LIST) == 2


def test_selected_page_id_none_when_unmarked() -> None:
    assert M.pages.selected_page_id("## Pages\n1: https://x\n2: https://y\n") is None


def test_selected_page_id_empty() -> None:
    assert M.pages.selected_page_id("") is None


def test_parse_evaluate_response_reads_the_fenced_json() -> None:
    response = 'Script ran on page and returned:\n```json\n{"a": [1, 2]}\n```'
    assert M.pages.parse_evaluate_response(response) == {"a": [1, 2]}


def test_parse_evaluate_response_keeps_backticks_inside_the_content() -> None:
    answer = "```python\nprint(1)\n```"
    response = f"Script ran on page and returned:\n```json\n{json.dumps(answer)}\n```"
    assert M.pages.parse_evaluate_response(response) == answer


def test_parse_evaluate_response_without_a_block_raises() -> None:
    with pytest.raises(RuntimeError, match="no JSON block"):
        M.pages.parse_evaluate_response("Error: the script threw")


_SNAPSHOT = 'uid=1_1 button "Start dictation"\nuid=1_2 button "Start research"\nuid=1_3 link "Docs"'


def test_find_clickable_exact_pass_beats_an_earlier_substring_hit() -> None:
    hit = M.pages.find_clickable(_SNAPSHOT, ["Start", "Start research"])
    assert hit is not None and (hit.uid, hit.label) == ("1_2", "Start research")


def test_find_clickable_falls_back_to_a_short_substring_case_insensitively() -> None:
    hit = M.pages.find_clickable('uid=1_1 button "Open the Upload Files menu"', ["upload files"])
    assert hit is not None and hit.uid == "1_1"


def test_find_clickable_ignores_a_long_container_label() -> None:
    label = "x" * 70 + " upload files"
    assert M.pages.find_clickable(f'uid=1_1 button "{label}"', ["upload files"]) is None


def test_find_clickable_none_when_nothing_matches() -> None:
    assert M.pages.find_clickable(_SNAPSHOT, ["Export"]) is None


def test_is_login_wall_matches_an_indicator_case_insensitively() -> None:
    assert M.pages.is_login_wall("Welcome back — LOG IN TO CHATGPT", ["Log in to ChatGPT"])
    assert not M.pages.is_login_wall("a page with a composer", ["Log in to ChatGPT"])


def test_check_inject_result_accepts_a_registered_prompt() -> None:
    M.pages.check_inject_result("ChatGPT", {"ok": True, "got_len": 5})


def test_check_inject_result_names_the_reason_when_the_composer_is_missing() -> None:
    with pytest.raises(
        RuntimeError, match=r"ChatGPT: failed to inject prompt \(composer-not-found\)"
    ):
        M.pages.check_inject_result("ChatGPT", {"ok": False, "reason": "composer-not-found"})


def test_check_inject_result_rejects_an_empty_read_back() -> None:
    with pytest.raises(RuntimeError, match="did not register"):
        M.pages.check_inject_result("ChatGPT", {"ok": True, "got_len": 0})


def test_submit_via_returns_the_path_that_fired() -> None:
    assert M.pages.submit_via("ChatGPT", {"ok": True, "via": "enter"}) == "enter"


def test_submit_via_raises_when_there_was_no_target() -> None:
    with pytest.raises(RuntimeError, match=r"could not submit \(no-send-target\)"):
        M.pages.submit_via("ChatGPT", {"ok": False, "reason": "no-send-target"})


def test_chat_id_from_url_extracts_chatgpt_id() -> None:
    assert M.sites.chat_id_from_url("chatgpt", "https://chatgpt.com/c/abc-123") == "abc-123"


def test_chat_id_from_url_extracts_claude_id() -> None:
    assert M.sites.chat_id_from_url("claude", "https://claude.ai/chat/xyz-789") == "xyz-789"


def test_chat_id_from_url_tries_every_pattern_of_the_site() -> None:
    assert M.sites.chat_id_from_url("perplexity", "https://www.perplexity.ai/thread/t-1") == "t-1"


def test_chat_id_from_url_returns_none_when_no_pattern_matches() -> None:
    assert M.sites.chat_id_from_url("chatgpt", "https://chatgpt.com/") is None


def test_chat_id_from_url_returns_none_for_unknown_site() -> None:
    assert M.sites.chat_id_from_url("chatgpt", "https://other.com/page") is None


def test_chat_url_builds_chatgpt_url() -> None:
    assert M.sites.chat_url("chatgpt", "abc-123") == "https://chatgpt.com/c/abc-123"


def test_chat_url_builds_claude_url() -> None:
    assert M.sites.chat_url("claude", "xyz-789") == "https://claude.ai/chat/xyz-789"


@pytest.mark.parametrize("name", sorted(M.sites.SITES))
def test_every_site_round_trips_a_chat_id_through_its_url(name: str) -> None:
    assert M.sites.chat_id_from_url(name, M.sites.chat_url(name, "abc-123")) == "abc-123"


@pytest.mark.parametrize("name", sorted(M.sites.SITES))
def test_every_site_profile_is_complete(name: str) -> None:
    prof = M.sites.site(name)
    assert prof.new_chat_url.startswith(f"https://{prof.host}")
    assert prof.composer and prof.send and prof.stop and prof.attach_path and prof.auto_login


def test_site_unknown_name_lists_the_known_sites() -> None:
    with pytest.raises(ValueError, match="unknown site 'bard'; known: chatgpt, gemini, claude"):
        M.sites.site("bard")


def test_claude_reads_the_rendered_markdown_through_a_script() -> None:
    answer = M.sites.site("claude").answer
    assert isinstance(answer, M.sites.ScriptAnswer) and ".standard-markdown" in answer.script
    assert isinstance(M.sites.site("chatgpt").answer, M.sites.SelectorAnswer)


def test_assert_same_site_accepts_the_sites_own_urls() -> None:
    M.sites.assert_same_site("https://chatgpt.com/c/abc", "chatgpt")
    M.sites.assert_same_site("https://chatgpt.com", "chatgpt")


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/c/abc",
        "https://chatgpt.com.evil.example/c/abc",
        "http://chatgpt.com/c/abc",
        "https://claude.ai/chat/abc",
    ],
)
def test_assert_same_site_refuses_another_host(url: str) -> None:
    with pytest.raises(ValueError, match=r"refusing to navigate to non-chatgpt\.com URL"):
        M.sites.assert_same_site(url, "chatgpt")


def test_any_present_js_embeds_the_selectors_as_json() -> None:
    selectors = ['a[href="x"]', "#b"]
    assert json.dumps(selectors) in M.js.any_present_js(selectors)


def test_inject_prompt_js_embeds_the_text_as_json_never_spliced() -> None:
    text = 'say "hi"\nthen `code`'
    script = M.js.inject_prompt_js(["#c"], text)
    assert json.dumps(text) in script
    assert text not in script


def test_submit_js_tries_the_send_buttons_before_the_composer() -> None:
    script = M.js.submit_js(["button.send"], ["#c"])
    assert script.index(json.dumps(["button.send"])) < script.index(json.dumps(["#c"]))


def test_read_answer_js_of_a_script_reader_is_the_script_itself() -> None:
    assert M.js.read_answer_js(M.sites.ScriptAnswer("() => 'x'")) == "() => 'x'"


def test_read_answer_js_of_a_selector_reader_reads_the_last_match() -> None:
    script = M.js.read_answer_js(M.sites.SelectorAnswer(("p.a", "p.b")))
    assert json.dumps(["p.a", "p.b"]) in script and "els[els.length - 1].innerText" in script


def test_chip_visible_js_carries_the_file_name_and_the_image_baseline() -> None:
    script = M.js.chip_visible_js("note.txt", ["https://x/a.png"])
    assert json.dumps("note.txt") in script and json.dumps(["https://x/a.png"]) in script


def test_send_enabled_js_checks_aria_disabled_too() -> None:
    assert "aria-disabled" in M.js.send_enabled_js(["button.send"])


def _fold(looks: list[tuple[str, bool, float]], *, stable_secs: float, state: Any = None) -> Any:
    current = M.idle.IdleState() if state is None else state
    for text, streaming, now in looks:
        current = M.idle.fold_poll(
            current, text=text, streaming=streaming, now=now, stable_secs=stable_secs
        )
    return current


# meiva:note
# Constant non-empty text that never streamed (no stop button, grew only once from "") must NOT read as a finished answer: the guard holds.
def test_idle_step_placeholder_never_settles() -> None:
    state = _fold([("Thinking", False, float(t)) for t in range(6)], stable_secs=0.0)
    assert not state.settled and state.stable_since is None


def test_idle_empty_text_never_settles() -> None:
    state = _fold([("", True, 0.0), ("", False, 1.0), ("", False, 2.0)], stable_secs=0.0)
    assert not state.settled


# meiva:note
# The stop control was seen while the text grew; once it is gone and the text is stable, the window opens at that look and the answer settles when `stable_secs` have passed.
def test_idle_step_settles_after_stable_when_stop_button_seen() -> None:
    state = _fold(
        [("partial", True, 0.0), ("final answer", True, 1.0), ("final answer", False, 2.0)],
        stable_secs=3.0,
    )
    assert not state.settled and state.stable_since == 2.0
    state = _fold([("final answer", False, 6.0)], stable_secs=3.0, state=state)
    assert state.settled and state.answer.complete and state.answer.text == "final answer"


# meiva:note
# The stop selector drifted (never True); two growth steps still let the answer converge.
def test_idle_step_settles_via_growth_without_stop_button() -> None:
    state = _fold([("a", False, 0.0), ("ab", False, 1.0), ("abc", False, 2.0)], stable_secs=2.0)
    assert not state.settled and state.growth_steps == 3
    state = _fold([("abc", False, 3.0)], stable_secs=2.0, state=state)
    assert not state.settled and state.stable_since == 3.0
    state = _fold([("abc", False, 5.0)], stable_secs=2.0, state=state)
    assert state.settled and state.answer.text == "abc"


# meiva:note
# The control flickers off for one look between two phases of an answer: the window opened there must not carry over, or the answer settles on the look right after the control comes back down.
def test_idle_a_look_with_the_stop_control_up_restarts_the_stability_window() -> None:
    state = _fold([("ab", True, 0.0), ("abc", True, 1.0), ("abc", False, 2.0)], stable_secs=1.0)
    assert state.stable_since == 2.0
    state = _fold([("abc", True, 3.0)], stable_secs=1.0, state=state)
    assert state.stable_since is None and not state.settled
    state = _fold([("abc", False, 4.0)], stable_secs=1.0, state=state)
    assert not state.settled and state.stable_since == 4.0
    state = _fold([("abc", False, 5.0)], stable_secs=1.0, state=state)
    assert state.settled


def test_idle_changed_text_restarts_the_window_and_only_growth_counts() -> None:
    state = _fold([("abcd", True, 0.0), ("abcd", False, 1.0)], stable_secs=5.0)
    assert state.stable_since == 1.0 and state.growth_steps == 1
    state = _fold([("ab", False, 2.0)], stable_secs=5.0, state=state)
    assert state.last == "ab" and state.stable_since is None and state.growth_steps == 1


def test_idle_a_settled_answer_is_final() -> None:
    state = _fold([("a", True, 0.0), ("a", False, 1.0), ("a", False, 2.0)], stable_secs=0.0)
    assert state.settled
    later = _fold([("something else", True, 9.0)], stable_secs=0.0, state=state)
    assert later is state


def test_next_poll_delay_is_the_poll_until_the_deadline_passes() -> None:
    assert M.pacing.next_poll_delay(now=1.0, deadline=2.0, poll=0.7) == 0.7
    assert M.pacing.next_poll_delay(now=2.0, deadline=2.0, poll=0.7) is None
    assert M.pacing.next_poll_delay(now=3.0, deadline=2.0, poll=0.7) is None


def test_idle_state_answer_carries_the_best_partial_while_unsettled() -> None:
    state = _fold([("still going", True, 0.0)], stable_secs=1.0)
    assert (state.answer.text, state.answer.complete) == ("still going", False)


def test_standard_pace_is_the_timing_the_driver_always_used() -> None:
    pace = M.pacing.STANDARD_PACE
    assert (pace.page_settle, pace.composer_wait, pace.editor_settle) == (2.5, 30.0, 0.4)
    assert (pace.answer_stable, pace.answer_poll) == (3.5, 0.7)


def test_answer_row_carries_label_chat_id_and_tab_state() -> None:
    row = M.rows.answer_row(
        name="chatgpt",
        via="button:send",
        answer=M.idle.Answer(text="hi", complete=True),
        url=CONVERSATION,
        tab_kept=False,
    )
    assert row == {
        "site": "chatgpt",
        "label": "ChatGPT",
        "submitted_via": "button:send",
        "answer": "hi",
        "complete": True,
        "url": CONVERSATION,
        "chat_id": "abc",
        "tab_kept": False,
    }


def test_answer_row_chat_id_is_none_off_a_conversation_url() -> None:
    row = M.rows.answer_row(
        name="chatgpt",
        via="enter",
        answer=M.idle.Answer(text="", complete=False),
        url=CHATGPT,
        tab_kept=True,
    )
    assert row["chat_id"] is None


def test_failed_row_names_the_site_and_the_error() -> None:
    assert M.rows.failed_row("gemini", "RuntimeError: boom") == {
        "site": "gemini",
        "label": "Gemini",
        "error": "RuntimeError: boom",
    }


@pytest.mark.parametrize(
    ("wait", "keep_tab", "kept"),
    [(True, False, False), (True, True, True), (False, False, True), (False, True, True)],
)
def test_keeps_tab_only_when_asked_or_when_the_answer_is_still_streaming(
    wait: bool, keep_tab: bool, kept: bool
) -> None:
    assert M.rows.keeps_tab(wait=wait, keep_tab=keep_tab) is kept


def test_tabs_left_open_skips_the_already_closed_in_setup_order() -> None:
    assert M.rows.tabs_left_open([2, 3, 4], {2}, keep_tab=False) == [3, 4]


def test_tabs_left_open_is_empty_when_the_caller_keeps_the_tabs() -> None:
    assert M.rows.tabs_left_open([2, 3], set(), keep_tab=True) == []


def test_cluster_label_strips_leading_dots() -> None:
    assert M.landing.cluster_label(Path("/home/user/.meiva-t1")) == "meiva-t1"
    assert M.landing.cluster_label(Path("/home/user/.meiva")) == "meiva"


def test_cluster_label_defaults_to_meiva_when_home_name_is_all_dots() -> None:
    assert M.landing.cluster_label(Path("/home/user/...")) == "meiva"


def test_capability_dir_is_under_downloads_by_home_label() -> None:
    places = M.landing.Places(home=Path("/u/.meiva-t1"), downloads=Path("/u/Downloads"))
    assert M.landing.capability_dir(places, "console") == Path(
        "/u/Downloads/meiva_meiva-t1_web-ai/console"
    )


def test_format_stamp_is_the_local_time_to_the_second() -> None:
    moment = datetime.datetime(2026, 10, 24, 9, 5, 7, tzinfo=datetime.UTC)
    assert M.landing.format_stamp(moment) == "20261024-090507"


def test_newest_new_download_ignores_what_was_there_and_takes_the_newest() -> None:
    entries = [
        M.landing.DownloadEntry("old.md", 1.0, 5),
        M.landing.DownloadEntry("a.md", 2.0, 5),
        M.landing.DownloadEntry("b.md", 3.0, 5),
    ]
    found = M.landing.newest_new_download({"old.md"}, entries)
    assert found is not None and found.name == "b.md"
    assert M.landing.newest_new_download({"old.md", "a.md", "b.md"}, entries) is None


def test_download_settled_needs_a_non_empty_unchanged_file() -> None:
    assert M.landing.download_settled(10, 10)
    assert not M.landing.download_settled(10, 11)
    assert not M.landing.download_settled(0, 0)
    assert not M.landing.download_settled(10, None)
