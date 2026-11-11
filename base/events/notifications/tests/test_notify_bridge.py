from __future__ import annotations

import json
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPT = _REPO_ROOT / "ui" / "app" / "src-tauri" / "scripts" / "notify-bridge.js"

# meiva:note
# Each case runs the checked-in injected bridge against a fake gateway: a scripted live stream
# (EventSource), a scripted catch-up page (fetch) and the native `app_notify` command, and reads
# back which native calls the bridge made. The page state (visibility, focus, open agent) is part
# of the scenario.
# meiva:link tests/docs/tests.md
_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const scenario = JSON.parse(process.argv[1]);
const calls = [];
const sources = [];
const storage = {};

function invoke(command, args) {
  if (command === "app_notify") calls.push(args);
  return Promise.resolve(command === "app_take_pending_click" ? false : {});
}

function EventSource() {
  sources.push(this);
}

const window = {
  __MEIVA_APP__: {
    notifications: true,
    gatewayUrl: "http://gateway.test",
    entryUrl: "http://gateway.test/",
    invoke: invoke,
  },
  location: { protocol: "https:", hostname: "console.test", search: scenario.search || "" },
  localStorage: {
    getItem: (key) => (key in storage ? storage[key] : null),
    setItem: (key, value) => {
      storage[key] = String(value);
    },
  },
  addEventListener() {},
  removeEventListener() {},
  setTimeout: (fn) => setTimeout(fn, 0),
  clearTimeout,
};
window.top = window;

const context = {
  window: window,
  location: window.location,
  document: {
    visibilityState: scenario.visible === false ? "hidden" : "visible",
    hidden: scenario.visible === false,
    hasFocus: () => scenario.focused !== false,
    addEventListener() {},
  },
  EventSource: EventSource,
  URLSearchParams: URLSearchParams,
  URL: URL,
  fetch: () =>
    Promise.resolve({
      ok: true,
      json: () => Promise.resolve(scenario.catchUp || { notifications: [], latest_id: 0 }),
    }),
  console: { error() {}, log() {} },
  setTimeout: (fn) => setTimeout(fn, 0),
};
EventSource.CLOSED = 2;
if (scenario.cursor) storage["meiva.notify.cursor"] = String(scenario.cursor);

vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), context);
const tick = () => new Promise((resolve) => setTimeout(resolve, 5));

(async function () {
  await tick();
  const source = sources[0];
  source.readyState = 1;
  source.onopen();
  await tick();
  await tick();
  for (const event of scenario.live || []) {
    source.onmessage({ data: JSON.stringify(event) });
  }
  await tick();
  await tick();
  process.stdout.write(
    JSON.stringify(calls.map((c) => [c.mode, c.tag, c.title, c.body])),
  );
})();
"""


def _event(
    notification_id: int,
    action: str,
    tag: str = "unread:7",
    *,
    subject: str = "Builder",
    body: str = "output",
    category: str = "core.unread",
    agent_id: int = 7,
) -> dict[str, object]:
    return {
        "role": "notification",
        "notification_id": notification_id,
        "category": category,
        "action": action,
        "tag": tag,
        "subject": subject,
        "body": body,
        "href": f"/?agent_id={agent_id}",
        "importance": "normal",
        "agent_id": agent_id,
    }


def _run(**scenario: object) -> list[list[str]]:
    # meiva:note
    # The bridge script runs under node with a canned gateway; the scenario is the only input.
    # meiva:link ui/app/docs/notifications.md
    result = subprocess.run(  # noqa: S603
        ["node", "-e", _HARNESS, json.dumps(scenario), str(_SCRIPT)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def test_the_first_unread_alerts_and_later_output_replaces_it_silently() -> None:
    calls = _run(
        live=[
            _event(1, "alert", body="first"),
            _event(2, "replace", body="second"),
        ]
    )
    assert calls == [
        ["alert", "unread:7", "Builder", "first"],
        ["replace", "unread:7", "Builder", "second"],
    ]


def test_a_retract_takes_the_notification_down_and_the_next_round_alerts_again() -> None:
    calls = _run(
        live=[
            _event(1, "alert", body="first"),
            _event(2, "retract", body=""),
            _event(3, "alert", body="again"),
        ]
    )
    assert [call[0] for call in calls] == ["alert", "retract", "alert"]
    assert calls[1][1] == "unread:7"


def test_a_replace_for_a_notification_never_raised_here_alerts_once() -> None:
    calls = _run(live=[_event(5, "replace", body="newer")])
    assert calls == [["alert", "unread:7", "Builder", "newer"]]


def test_the_agent_the_user_is_looking_at_is_not_notified() -> None:
    calls = _run(search="?agent_id=7", live=[_event(1, "alert")])
    assert calls == []


def test_another_agent_or_a_background_window_is_still_notified() -> None:
    assert len(_run(search="?agent_id=8", live=[_event(1, "alert")])) == 1
    assert len(_run(search="?agent_id=7", focused=False, live=[_event(1, "alert")])) == 1
    assert len(_run(search="?agent_id=7", visible=False, live=[_event(1, "alert")])) == 1


def test_a_question_alerts_even_while_its_agent_is_open() -> None:
    question = _event(
        1, "alert", "question:31", subject="Deploy now?", body="Builder", category="core.question"
    )
    assert _run(search="?agent_id=7", live=[question]) == [
        ["alert", "question:31", "Deploy now?", "Builder"]
    ]


def test_each_question_is_its_own_notification() -> None:
    first = _event(1, "alert", "question:31", subject="A?", category="core.question")
    second = _event(2, "alert", "question:32", subject="B?", category="core.question")
    third = _event(3, "retract", "question:31", subject="A?", category="core.question")
    calls = _run(live=[first, second, third])
    assert [(call[0], call[1]) for call in calls] == [
        ("alert", "question:31"),
        ("alert", "question:32"),
        ("retract", "question:31"),
    ]


def test_a_redelivered_event_is_shown_once() -> None:
    assert len(_run(live=[_event(1, "alert"), _event(1, "alert")])) == 1


def test_a_catch_up_gap_folds_to_the_final_state_of_each_notification() -> None:
    gap = {
        "latest_id": 9,
        "notifications": [
            {**_event(2, "alert", body="one"), "id": 2},
            {**_event(3, "replace", body="two"), "id": 3},
            {**_event(4, "alert", "question:31", subject="A?", category="core.question"), "id": 4},
            {
                **_event(5, "retract", "question:31", subject="A?", category="core.question"),
                "id": 5,
            },
            {**_event(6, "replace", body="three"), "id": 6},
            {**_event(7, "alert", "unread:8", agent_id=8, body="gone"), "id": 7},
            {**_event(8, "retract", "unread:8", agent_id=8, body=""), "id": 8},
        ],
    }
    calls = _run(cursor=1, catchUp=gap)
    assert calls == [
        ["retract", "question:31", "", ""],
        ["alert", "unread:7", "Builder", "three"],
        ["retract", "unread:8", "", ""],
    ]


def test_a_catch_up_notifies_once_for_a_tag_that_still_exists() -> None:
    gap = {
        "latest_id": 3,
        "notifications": [
            {**_event(2, "alert", body="one"), "id": 2},
            {**_event(3, "replace", body="two"), "id": 3},
        ],
    }
    assert _run(cursor=1, catchUp=gap) == [["alert", "unread:7", "Builder", "two"]]


def test_a_catch_up_row_already_shown_live_is_not_shown_again() -> None:
    gap = {"latest_id": 1, "notifications": [{**_event(1, "alert"), "id": 1}]}
    assert _run(cursor=1, catchUp=gap, live=[_event(1, "alert")]) == [
        ["alert", "unread:7", "Builder", "output"]
    ]
