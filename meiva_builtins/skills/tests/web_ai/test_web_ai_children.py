# meiva:note
# Tests of the deep-research and media children of the web-ai skill that need no page: the helpers that build a file name, an extension and a hardened curl argv or judge downloaded bytes, the report landing under the run's places, and the `reply` command against a stand-in browser behind a real MCP daemon socket.
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from base.host.tests.short_tmp import short_tmp as short_tmp
from meiva.mcps.tests.stub_daemon import stub_daemon as stub_daemon
from meiva_builtins.skills.tests.skill_script import load_skill_script
from meiva_builtins.skills.tests.web_ai.chrome_stand_in import (
    PageScript,
    ServeChrome,
    StandInChrome,
    known_sites,
)
from meiva_builtins.skills.tests.web_ai.chrome_stand_in import serve_chrome as serve_chrome
from meiva_builtins.skills.tests.web_ai.driver_modules import load_driver_modules

M = load_driver_modules()
research = load_skill_script(
    "integrations", "web-ai", "deep-research", "scripts", "research.py", name="web_ai_research"
)
generate = load_skill_script(
    "integrations", "web-ai", "media", "scripts", "generate.py", name="web_ai_generate"
)

CONVERSATION = "https://chatgpt.com/c/abc"


def test_research_slug_keeps_the_first_words_of_the_report() -> None:
    assert research._slug("# The State of Rust, 2026!") == "the-state-of-rust-2026"
    assert research._slug("!!!") == "research"


def test_research_cfg_names_the_supported_sites_for_an_unknown_one() -> None:
    with pytest.raises(ValueError, match="deep-research supports gemini, chatgpt, perplexity"):
        research._cfg("claude")


def test_research_save_lands_the_report_and_meta_under_the_capability_directory(
    tmp_path: Path,
) -> None:
    places = M.landing.Places(home=tmp_path / ".meiva-t1", downloads=tmp_path / "Downloads")

    outdir = research._save("gemini", CONVERSATION, "# Findings\n\nbody", places)

    assert outdir.parent == tmp_path / "Downloads" / "meiva_meiva-t1_web-ai" / "deep-research"
    assert outdir.name.endswith("-gemini-findings-body")
    assert (outdir / "report.md").read_text(encoding="utf-8") == "# Findings\n\nbody"
    assert '"chars": 16' in (outdir / "meta.json").read_text(encoding="utf-8")


def test_research_reply_types_the_answer_into_the_conversation_and_keeps_the_tab(
    serve_chrome: ServeChrome,
) -> None:
    chrome = serve_chrome(
        StandInChrome(
            js=M.js,
            profiles=known_sites(M.sites, M.js),
            pages={CONVERSATION: PageScript(after_submit_url=None)},
        )
    )
    args = argparse.Namespace(site="chatgpt", prompt="focus on 2025", url=CONVERSATION)

    result = research.cmd_reply(args)

    assert result == {
        "site": "chatgpt",
        "url": CONVERSATION,
        "submitted_via": "button:send",
        "state": "replied",
    }
    assert chrome.tabs[2].typed == "focus on 2025" and chrome.tabs[2].submitted


def test_generate_slug_defaults_to_media() -> None:
    assert generate._slug("A photorealistic red panda!") == "a-photorealistic-red-panda"
    assert generate._slug("???") == "media"


@pytest.mark.parametrize(
    ("src", "ext"),
    [
        ("https://cdn.example/a/b.PNG?x=1", "png"),
        ("https://cdn.example/clip.mp4#t=3", "mp4"),
        ("https://cdn.example/asset", "bin"),
    ],
)
def test_generate_ext_from_url_reads_a_known_media_suffix(src: str, ext: str) -> None:
    assert generate._ext_from_url(src, default="bin") == ext


def test_generate_curl_args_pin_https_and_bound_redirects_size_and_time(tmp_path: Path) -> None:
    dest = tmp_path / "image.png"

    args = generate._curl_args(
        "https://cdn.example/a.png", dest, max_filesize="100M", max_time="120"
    )

    assert args[:2] == ["curl", "-fsSL"]
    assert args[args.index("--proto") + 1] == "=https"
    assert args[args.index("--proto-redir") + 1] == "=https"
    assert args[args.index("--max-redirs") + 1] == "5"
    assert args[args.index("--max-filesize") + 1] == "100M"
    assert args[args.index("--max-time") + 1] == "120"
    assert args[-3:] == ["-o", str(dest), "https://cdn.example/a.png"]


def test_generate_refuses_an_html_page_posing_as_media(tmp_path: Path) -> None:
    page = tmp_path / "image.png"
    page.write_text("<!DOCTYPE html><html>sign in</html>", encoding="utf-8")

    with pytest.raises(RuntimeError, match="auth interstitial"):
        generate._assert_media_bytes(page)

    assert not page.exists()


def test_generate_keeps_real_media_bytes(tmp_path: Path) -> None:
    media = tmp_path / "image.png"
    media.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    generate._assert_media_bytes(media)

    assert media.exists()
