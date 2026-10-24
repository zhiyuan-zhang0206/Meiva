from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.host.tests.short_tmp import short_tmp as short_tmp
from meiva.mcps.tests.stub_daemon import stub_daemon as stub_daemon
from meiva_builtins.skills.tests.skill_script import load_skill_script
from meiva_builtins.skills.tests.web_ai.chrome_stand_in import (
    PageScript,
    ServeChrome,
    StandInChrome,
)
from meiva_builtins.skills.tests.web_ai.chrome_stand_in import serve_chrome as serve_chrome
from meiva_builtins.skills.tests.web_ai.driver_modules import load_driver_modules

M = load_driver_modules()
ask = load_skill_script(
    "integrations", "web-ai", "console", "scripts", "ask.py", name="web_ai_console_ask_under_test"
)

# meiva:note
# Waits that take no time: the stand-in browser answers at once.
FAST = M.pacing.Pacing(
    page_settle=0.0,
    composer_wait=0.3,
    editor_settle=0.0,
    answer_stable=0.0,
    answer_poll=0.0,
    ready_poll=0.01,
)


def test_to_row_error_row_passes_through_as_not_ok() -> None:
    row = ask._to_row(
        {"site": "claude", "label": "Claude", "error": "RuntimeError: composer not found"}
    )
    assert row == {
        "site": "claude",
        "label": "Claude",
        "ok": False,
        "error": "RuntimeError: composer not found",
    }


def test_to_row_completed_answer_is_ok() -> None:
    row = ask._to_row(
        {
            "site": "chatgpt",
            "label": "ChatGPT",
            "submitted_via": "button",
            "answer": "the answer",
            "complete": True,
            "url": "https://chatgpt.com/c/abc",
            "tab_kept": False,
        }
    )
    assert row["ok"] is True
    assert row["chars"] == len("the answer")
    assert row["answer"] == "the answer"


def test_to_row_incomplete_partial_is_not_ok() -> None:
    row = ask._to_row(
        {
            "site": "chatgpt",
            "label": "ChatGPT",
            "submitted_via": "button",
            "answer": "Thinking",
            "complete": False,
            "url": "https://chatgpt.com/c/abc",
            "tab_kept": False,
        }
    )
    assert row["ok"] is False
    assert row["complete"] is False


def test_to_row_empty_complete_answer_is_not_ok() -> None:
    row = ask._to_row(
        {
            "site": "chatgpt",
            "label": "ChatGPT",
            "submitted_via": "button",
            "answer": "",
            "complete": True,
            "url": "https://chatgpt.com/c/abc",
            "tab_kept": False,
        }
    )
    assert row["ok"] is False


def test_to_row_includes_chat_id_when_present() -> None:
    row = ask._to_row(
        {
            "site": "chatgpt",
            "label": "ChatGPT",
            "submitted_via": "button",
            "answer": "the answer",
            "complete": True,
            "url": "https://chatgpt.com/c/abc-123",
            "chat_id": "abc-123",
            "tab_kept": False,
        }
    )
    assert row["chat_id"] == "abc-123"


def test_to_row_chat_id_none_when_absent() -> None:
    row = ask._to_row(
        {
            "site": "chatgpt",
            "label": "ChatGPT",
            "submitted_via": "button",
            "answer": "the answer",
            "complete": True,
            "url": "https://chatgpt.com/c/abc-123",
            "tab_kept": False,
        }
    )
    assert row["chat_id"] is None


def _stand_in() -> StandInChrome:
    pages = {
        "https://chatgpt.com/": PageScript(
            after_submit_url="https://chatgpt.com/c/abc",
            answers=("partial", "chatgpt says four"),
            stops=(True, False),
        ),
        "https://gemini.google.com/app": PageScript(
            composer=False, snapshot="nothing the driver knows"
        ),
    }
    return StandInChrome(js=M.js, profiles=M.sites.SITES, pages=pages)


# meiva:note
# The whole console run against a stand-in browser and real files: one model answers, one has a page the driver cannot use. The answered model is `ok` with its conversation handle, the other is an `ok: False` row with the setup error, the answers land under the capability directory of the run's places, every owned tab is closed, and stdout carries the same JSON the files hold.
def test_main_asks_the_models_and_lands_the_answers(
    serve_chrome: ServeChrome, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    chrome = serve_chrome(_stand_in())
    places = M.landing.Places(home=tmp_path / ".meiva-t1", downloads=tmp_path / "Downloads")

    ask.main(
        ["--models", "chatgpt,gemini", "--prompt", "What is 2+2?", "--timeout", "5"],
        places=places,
        pace=FAST,
    )

    printed = json.loads(capsys.readouterr().out)
    chatgpt, gemini = printed["results"]
    assert (chatgpt["site"], chatgpt["ok"], chatgpt["chat_id"]) == ("chatgpt", True, "abc")
    assert chatgpt["answer"] == "chatgpt says four"
    assert gemini["ok"] is False and "composer not found" in gemini["error"]
    outdir = Path(printed["dir"])
    assert outdir.parent == tmp_path / "Downloads" / "meiva_meiva-t1_web-ai" / "console"
    assert json.loads((outdir / "result.json").read_text(encoding="utf-8")) == {
        "prompt": "What is 2+2?",
        "results": printed["results"],
    }
    assert "chatgpt says four" in (outdir / "answers.md").read_text(encoding="utf-8")
    assert sorted(chrome.tabs) == [1]
