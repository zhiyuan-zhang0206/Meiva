# meiva:note
# A stand-in for the chrome MCP behind a real MCP daemon socket, for the shell tests of the web-ai driver. The driver reaches the browser as `meiva.mcps.chrome.<tool>(...)`, so the SDK's own path runs untouched: the process context's MCP client dials the daemon socket under the test's home, and a stub daemon on that socket answers the chrome tools out of a small browser model (tabs with an id, a URL, a selected tab and a scripted page). The stand-in refuses what the real browser refuses: closing the last open page, closing or selecting a tab that is gone. A page script answers the evaluated scripts by comparing them with the script builders' own output, so a test fails loudly when the driver asks something the page does not know. `events` is what the browser saw, in order, `tabs` its state afterwards, and `socket` the path the SDK dials once the stand-in is served.
from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from base.paths import mcp_daemon_shared_socket
from base.paths.tests.homes import point_home
from meiva.mcps.tests.stub_daemon import Respond, StubDaemon, ok_line

_SENTINEL = "SENTINEL-TEXT"


# meiva:note
# What the stand-in reads from one site profile of the driver: the host that selects it, the selector lists its scripts are built from and the script that reads its answer. The profile classes live in the driver's loaded modules, which no test module can name, so the stand-in holds this value, read once by `known_sites`.
@dataclass(frozen=True)
class KnownSite:
    host: str
    composer: tuple[str, ...]
    send: tuple[str, ...]
    stop: tuple[str, ...]
    read_answer: str


# meiva:note
# The sites a page can be on, read from the driver's loaded `_sites` module; the answer script is the one the driver's `_js` module builds for the profile.
def known_sites(sites: ModuleType, js: ModuleType) -> dict[str, KnownSite]:
    return {
        name: KnownSite(
            host=profile.host,
            composer=tuple(profile.composer),
            send=tuple(profile.send),
            stop=tuple(profile.stop),
            read_answer=js.read_answer_js(profile.answer),
        )
        for name, profile in sites.SITES.items()
    }


# meiva:note
# How the page behind a URL behaves. `composer` is whether the composer is rendered, `snapshot` the accessibility tree the page shows, and `reveals_composer` the uids whose click makes the composer appear (a Google sign-in button), and `saves_on_click` the uid and file path of a control whose click makes the browser save a download. A submitted prompt takes the page to `after_submit_url`; from then on the n-th answer read returns `answers[n]` and the n-th stop-control read `stops[n]`, the last of each repeating. `send` is how a submit lands (`button`, `enter` or `none` for a page with no send target), `registers` whether the editor accepts a typed prompt, and `location_fails` and `answer_fails` make reading the URL or the answer fail the way a dead tab does.
@dataclass(frozen=True)
class PageScript:
    composer: bool = True
    snapshot: str = ""
    reveals_composer: tuple[str, ...] = ()
    saves_on_click: tuple[tuple[str, Path], ...] = ()
    send: str = "button"
    registers: bool = True
    after_submit_url: str | None = None
    answers: tuple[str, ...] = ("",)
    stops: tuple[bool, ...] = (False,)
    location_fails: bool = False
    answer_fails: bool = False


# meiva:note
# One open tab of the browser model: what the page has been told so far.
@dataclass
class OpenTab:
    id: int
    location: str
    script: PageScript
    profile: KnownSite | None
    composer: bool
    typed: str = ""
    submitted: bool = False
    answer_reads: int = 0
    stop_reads: int = 0
    uploads: list[str] = field(default_factory=list[str])


# meiva:note
# A refusal the browser answers with an error result, as the real tool does.
class RefusedError(Exception):
    pass


# meiva:note
# The chrome MCP's tools over a browser model. The user's own tab (id 1) is open from the start; `pages` maps a URL prefix to the script of the page it opens (the longest prefix wins, a plain page otherwise), `profiles` maps a name to the site profile whose scripts a page on that profile's host understands, and `js` is the loaded `_js` module whose builders define what the driver may ask. With `marks_selected=False` the page list names no selected tab, which is how the real tool's format drifts.
class StandInChrome:
    def __init__(
        self,
        *,
        js: ModuleType,
        profiles: Mapping[str, KnownSite],
        pages: Mapping[str, PageScript] | None = None,
        marks_selected: bool = True,
    ) -> None:
        self._js = js
        self._profiles = profiles
        self._pages = dict(pages or {})
        self._marks_selected = marks_selected
        self.tabs: dict[int, OpenTab] = {}
        self.selected = 1
        self.events: list[str] = []
        self.socket: Path | None = None
        self._next_id = 1
        self._tools: dict[str, Callable[[Mapping[str, object]], str]] = {
            "new_page": self._new_page,
            "close_page": self._close_page,
            "select_page": self._select_page,
            "evaluate_script": self._evaluate_script,
            "take_snapshot": self._take_snapshot,
            "click": self._click,
            "upload_file": self._upload_file,
        }
        self._open("https://user.example/", PageScript())

    def _script_for(self, url: str) -> PageScript:
        known = [prefix for prefix in self._pages if url.startswith(prefix)]
        return self._pages[max(known, key=len)] if known else PageScript()

    def _profile_for(self, url: str) -> KnownSite | None:
        return next((p for p in self._profiles.values() if f"//{p.host}" in url), None)

    def _open(self, url: str, script: PageScript) -> OpenTab:
        tab = OpenTab(
            id=self._next_id,
            location=url,
            script=script,
            profile=self._profile_for(url),
            composer=script.composer,
        )
        self.tabs[tab.id] = tab
        self._next_id += 1
        self.selected = tab.id
        return tab

    def _listing(self) -> str:
        lines = ["## Pages"]
        for tab in self.tabs.values():
            mark = " [selected]" if self._marks_selected and tab.id == self.selected else ""
            lines.append(f"{tab.id}: {tab.location}{mark}")
        return "\n".join(lines)

    def _existing(self, args: Mapping[str, object]) -> int:
        page_id = int(str(args["pageId"]))
        if page_id not in self.tabs:
            raise RefusedError(f"page {page_id} is not open")
        return page_id

    def _new_page(self, args: Mapping[str, object]) -> str:
        url = str(args["url"])
        self.events.append(f"new_page {url}")
        self._open(url, self._script_for(url))
        return self._listing()

    def _close_page(self, args: Mapping[str, object]) -> str:
        page_id = self._existing(args)
        self.events.append(f"close_page {page_id}")
        if len(self.tabs) == 1:
            raise RefusedError("The last open page cannot be closed.")
        del self.tabs[page_id]
        if self.selected == page_id:
            self.selected = next(iter(self.tabs))
        return self._listing()

    def _select_page(self, args: Mapping[str, object]) -> str:
        page_id = self._existing(args)
        self.events.append(f"select_page {page_id}")
        self.selected = page_id
        return self._listing()

    def _evaluate_script(self, args: Mapping[str, object]) -> str:
        tab = self.tabs[self.selected]
        kind, value = self._evaluate(tab, str(args["function"]))
        self.events.append(f"{kind} {tab.id}")
        return f"Script ran on page and returned:\n```json\n{json.dumps(value)}\n```"

    def _take_snapshot(self, args: Mapping[str, object]) -> str:
        tab = self.tabs[self.selected]
        self.events.append(f"take_snapshot {tab.id}")
        return tab.script.snapshot

    def _click(self, args: Mapping[str, object]) -> str:
        tab = self.tabs[self.selected]
        uid = str(args["uid"])
        self.events.append(f"click {tab.id} {uid}")
        if uid in tab.script.reveals_composer:
            tab.composer = True
        for saved_uid, path in tab.script.saves_on_click:
            if saved_uid == uid:
                path.write_text("downloaded", encoding="utf-8")
        return "Successfully clicked on the element"

    def _upload_file(self, args: Mapping[str, object]) -> str:
        tab = self.tabs[self.selected]
        name = Path(str(args["filePath"])).name
        self.events.append(f"upload_file {tab.id} {name}")
        tab.uploads.append(name)
        return "File uploaded"

    def _evaluate(self, tab: OpenTab, script: str) -> tuple[str, object]:
        profile = tab.profile
        if profile is None:
            raise RefusedError(f"no site profile for {tab.location}")
        js = self._js
        if script == js.LOCATION_JS:
            if tab.script.location_fails:
                raise RefusedError("the tab is gone")
            return "location", tab.location
        if script == js.any_present_js(profile.composer):
            return "composer", tab.composer
        if script == js.any_present_js(profile.stop):
            return "stop", self._next_stop(tab)
        if script == profile.read_answer:
            return "answer", self._next_answer(tab)
        if script == js.submit_js(profile.send, profile.composer):
            return "submit", self._submit(tab)
        if script == js.send_enabled_js(profile.send):
            return "send_enabled", True
        if script == js.IMAGE_SOURCES_JS:
            return "images", []
        before, after = js.inject_prompt_js(profile.composer, _SENTINEL).split(
            json.dumps(_SENTINEL)
        )
        if script.startswith(before) and script.endswith(after):
            text = json.loads(script[len(before) : len(script) - len(after)])
            return "inject", self._type(tab, text)
        if any(json.dumps(name) in script for name in tab.uploads):
            return "chip", True
        raise RefusedError(f"unsupported script: {script[:80]!r}")

    def _type(self, tab: OpenTab, text: str) -> dict[str, object]:
        if not tab.composer:
            return {"ok": False, "reason": "composer-not-found"}
        tab.typed = text if tab.script.registers else ""
        return {"ok": True, "got_len": len(tab.typed)}

    def _submit(self, tab: OpenTab) -> dict[str, object]:
        if tab.script.send == "none":
            return {"ok": False, "reason": "no-send-target"}
        tab.submitted = True
        if tab.script.after_submit_url is not None:
            tab.location = tab.script.after_submit_url
        return {"ok": True, "via": "enter" if tab.script.send == "enter" else "button:send"}

    def _next_answer(self, tab: OpenTab) -> str:
        if tab.script.answer_fails:
            raise RefusedError("the page stopped answering")
        if not tab.submitted:
            return ""
        answers = tab.script.answers
        text = answers[min(tab.answer_reads, len(answers) - 1)]
        tab.answer_reads += 1
        return text

    def _next_stop(self, tab: OpenTab) -> bool:
        if not tab.submitted:
            return False
        stops = tab.script.stops
        visible = stops[min(tab.stop_reads, len(stops) - 1)]
        tab.stop_reads += 1
        return visible

    def _run(self, tool: str, args: Mapping[str, object]) -> str:
        handler = self._tools.get(tool)
        if handler is None:
            raise RefusedError(f"unknown tool {tool!r}")
        return handler(args)

    def respond(self, request: dict[str, object]) -> Iterable[bytes]:
        if request["method"] == "list_tools":
            return [ok_line(request, [])]
        params = cast(dict[str, Any], request["params"])
        try:
            text = self._run(str(params["tool"]), params["args"])
        except RefusedError as refusal:
            return [ok_line(request, _result(str(refusal), is_error=True))]
        return [ok_line(request, _result(text, is_error=False))]


def _result(text: str, *, is_error: bool) -> dict[str, object]:
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
        "structuredContent": None,
    }


type ServeChrome = Callable[[StandInChrome], StandInChrome]


# meiva:note
# Serve a stand-in on the daemon socket of this test's home. A test module that uses it imports `short_tmp` and `stub_daemon` beside it, and the SDK fixtures come from the directory's path scope. The home is a short `/tmp` path (a unix socket path is bounded), the SDK context is the repo's own `sdk_environment`, and the stub daemon's socket is linked at the path `meiva.mcps` dials. The stand-in is returned so the test can read `events` and `tabs`.
@pytest.fixture
def serve_chrome(
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_daemon: Callable[[Respond], StubDaemon],
    sdk_environment: object,
) -> ServeChrome:
    home = point_home(monkeypatch, short_tmp)
    (home / "run").mkdir()

    def serve(stand_in: StandInChrome) -> StandInChrome:
        daemon = stub_daemon(stand_in.respond)
        stand_in.socket = Path(mcp_daemon_shared_socket(home))
        stand_in.socket.symlink_to(daemon.path)
        return stand_in

    return serve
