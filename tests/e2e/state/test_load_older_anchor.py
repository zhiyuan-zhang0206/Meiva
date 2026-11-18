from __future__ import annotations

import re
import time
from typing import TypedDict, cast

import pytest
from playwright.sync_api import Page, Route

from tests.e2e._credentials import gateway_get
from tests.e2e.fixture_environment import E2EEnv

_SAMPLER_JS = """
() => {
  const all = [...document.querySelectorAll('[data-slot="scroll-area-viewport"]')];
  const vp = all.find(v => v.querySelectorAll('[data-item-id]').length > 0);
  if (!vp) return {error: 'no timeline viewport'};
  window.__tl = { samples: [], vp };
  const sample = (why) => {
    const items = [...vp.querySelectorAll('[data-item-id]')];
    const vpRect = vp.getBoundingClientRect();
    // The reading position = the topmost visible real node's viewport top (falling
    // back to the 0.0 prompt card when only it fills the viewport).
    let anchorId = null, anchorTop = null;
    let fallbackId = null, fallbackTop = null;
    const tops = {};
    for (const n of items) {
      const id = n.dataset.itemId;
      if (!id || id.startsWith('_')) continue;
      const r = n.getBoundingClientRect();
      if (r.bottom >= vpRect.top && r.top <= vpRect.bottom) {
        tops[id] = r.top;
        if (id === '0.0') {
          if (fallbackId === null) { fallbackId = id; fallbackTop = r.top; }
          continue;
        }
        if (anchorId === null) { anchorId = id; anchorTop = r.top; }
      }
    }
    if (anchorId === null) { anchorId = fallbackId; anchorTop = fallbackTop; }
    if (window.__tl.samples.length > 20000) return;
    const measured = { t: Math.round(performance.now()), why, st: vp.scrollTop, sh: vp.scrollHeight, n: Number(vp.querySelector('[role=log]').dataset.timelineItemCount), anchorId, anchorTop, tops };
    window.__tl.samples.push(measured);
    return measured;
  };
  let raf = 0;
  const loop = () => { sample('raf'); raf = requestAnimationFrame(loop); };
  raf = requestAnimationFrame(loop);
  window.__tl.stop = () => cancelAnimationFrame(raf);
  vp.addEventListener('scroll', () => sample('scroll'), { passive: true });
  window.__tl.sampleNow = () => sample('before-pull');
  sample('init');
  return {ok: true, n: Number(vp.querySelector('[role=log]').dataset.timelineItemCount), sh: vp.scrollHeight};
}
"""

_SCROLL_UP_JS = """
() => {
  const vp = window.__tl.vp;
  const boundary = 0;
  window.__tl.arrivalEnded = vp.scrollTop <= boundary;
  const ended = event => {
    if (event.target === vp && vp.scrollTop <= boundary + 1) {
      window.__tl.arrivalEnded = true;
      vp.removeEventListener('scrollend', ended);
    }
  };
  vp.addEventListener('scrollend', ended);
  let st = vp.scrollTop;
  const step = () => {
    st = Math.max(boundary, st - 500);
    vp.scrollTop = st;
    if (st > boundary) setTimeout(step, 30);
  };
  step();
  return 'scrolling';
}
"""


class _ReadingSample(TypedDict):
    anchorId: str | None
    anchorTop: float | None
    tops: dict[str, float]
    st: float
    sh: float
    n: int


def _reading_top(sample: _ReadingSample, anchor_id: str | None = None) -> float:
    target = anchor_id if anchor_id is not None else sample["anchorId"]
    top = sample["tops"].get(target, sample["anchorTop"]) if target else sample["anchorTop"]
    assert top is not None
    return top


# meiva:note
# Verify the captured reading row remains visible and stationary on every post-landing sample.
def _assert_anchor_samples(
    after: list[_ReadingSample], anchor_id: str, before_top: float, round_no: int
) -> None:
    for s in after:
        assert anchor_id in s["tops"], (
            f"round {round_no}: reading anchor {anchor_id} left the viewport across the landing "
            f"(st={s['st']}, sh={s['sh']}, n={s['n']}, visible={list(s['tops'])})"
        )
        top = _reading_top(s, anchor_id)
        assert abs(top - before_top) < 3.0, (
            f"round {round_no}: reading position moved by "
            f"{top - before_top:.1f}px across the landing "
            f"(before={before_top:.1f}, after={top:.1f}, "
            f"st={s['st']}, sh={s['sh']}, n={s['n']}, anchor={anchor_id})"
        )


def _assert_landing_holds(page: Page, sample_start: int, round_no: int) -> None:
    samples: list[_ReadingSample] = page.evaluate("window.__tl.samples")[sample_start:]
    landing_idx = next(
        (i for i, s in enumerate(samples) if i > 0 and s["n"] != samples[i - 1]["n"]),
        None,
    )
    assert landing_idx is not None, f"round {round_no}: no landing frame in the samples"
    before = [s for s in samples[:landing_idx] if s["anchorTop"] is not None]
    after = [s for s in samples[landing_idx:] if s["anchorTop"] is not None]
    assert before and after, f"round {round_no}: no anchor measurements around the landing"
    anchor_id = before[-1]["anchorId"]
    assert anchor_id is not None
    _assert_anchor_samples(after, anchor_id, _reading_top(before[-1], anchor_id), round_no)


def _wait_landing(
    page: Page,
    before_n: int,
    round_no: int,
    agent_id: int,
    gateway_url: str,
    before_requests: list[str],
) -> int:
    deadline = time.monotonic() + 30.0
    n = before_n
    while time.monotonic() < deadline:
        n = page.evaluate(
            "Number(window.__tl.vp.querySelector('[role=log]').dataset.timelineItemCount)"
        )
        if n > before_n:
            return n
        page.wait_for_timeout(200)
    rest_items = gateway_get(
        f"{gateway_url}/api/agents/{agent_id}/timeline?limit=1000",
        timeout=30.0,
    ).json()["items"]
    samples = page.evaluate("window.__tl.samples")
    raise AssertionError(
        f"round {round_no}: the older window never landed: "
        f"rest_total={len(rest_items)}, before_requests={before_requests}, "
        f"last_samples={samples[-6:]}"
    )


def _run_rounds(
    page: Page,
    before_requests: list[str],
    paused_routes: list[Route],
    init_n: int,
    agent_id: int,
    gateway_url: str,
) -> int:
    sample_start = page.evaluate("window.__tl.samples.length")
    _load_older_at_top(page, before_requests, paused_routes)
    _wait_landing(page, init_n, 1, agent_id, gateway_url, before_requests)
    page.wait_for_timeout(1200)
    _assert_landing_holds(page, sample_start, 1)

    round_no = 1
    while round_no < 5:
        round_no += 1
        page.evaluate("window.__tl.vp.scrollTop = 240")
        page.evaluate("window.__tl.vp.dispatchEvent(new Event('scroll'))")
        page.wait_for_timeout(200)
        before_n = page.evaluate(
            "Number(window.__tl.vp.querySelector('[role=log]').dataset.timelineItemCount)"
        )
        sample_start = page.evaluate("window.__tl.samples.length")
        did_pull = _pull_history_page(page, before_requests)

        deadline = time.monotonic() + (30.0 if did_pull else 0.0)
        n = before_n
        while time.monotonic() < deadline:
            n = page.evaluate(
                "Number(window.__tl.vp.querySelector('[role=log]').dataset.timelineItemCount)"
            )
            if n > before_n:
                break
            page.wait_for_timeout(200)
        if n <= before_n:
            rest = gateway_get(
                f"{gateway_url}/api/agents/{agent_id}/timeline?limit=1000",
                timeout=30.0,
            ).json()["items"]
            if before_n < len(rest):
                raise AssertionError(
                    f"round {round_no}: a released pull did not load more history; "
                    f"mounted={before_n} rest_total={len(rest)}, "
                    f"last_samples={page.evaluate('window.__tl.samples')[-6:]}"
                )
            break
        page.wait_for_timeout(1200)
        _assert_landing_holds(page, sample_start, round_no)
    return round_no


def _pull_history_page(page: Page, before_requests: list[str]) -> bool:
    count = len(before_requests)
    page.evaluate(_SCROLL_UP_JS)
    page.wait_for_function("() => window.__tl.arrivalEnded", timeout=15_000)
    assert len(before_requests) == count, "arriving at the top loaded history without a pull"
    if page.get_by_test_id("load-older-keyboard").count() == 0:
        return False
    viewport = page.locator('[data-slot="scroll-area-viewport"]').filter(
        has=page.locator('[role="log"]')
    )
    box = viewport.bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.wheel(0, -100)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and len(before_requests) == count:
        page.wait_for_timeout(50)
    assert len(before_requests) == count + 1, "a deliberate pull must request exactly one page"
    return True


def _reject_first_page(page: Page, before_requests: list[str], paused_routes: list[Route]) -> None:
    _ = page.evaluate(_SCROLL_UP_JS)
    _ = page.wait_for_function("() => window.__tl.arrivalEnded")
    sample = cast(_ReadingSample, page.evaluate("window.__tl.sampleNow()"))
    anchor = sample["anchorId"]
    assert anchor is not None
    start = cast(int, page.evaluate("window.__tl.samples.length"))

    assert _pull_history_page(page, before_requests)
    paused_routes.pop().fulfill(
        status=503,
        json={"detail": "Checkpoint history unavailable"},
        content_type="application/json",
    )
    page.get_by_text("Failed to load older messages: Checkpoint history unavailable").wait_for()
    _ = page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )

    after = cast(list[_ReadingSample], page.evaluate("window.__tl.samples"))[start:]
    assert after
    _assert_anchor_samples(after, anchor, _reading_top(sample, anchor), 0)
    assert all(s["n"] == sample["n"] for s in after)

    # meiva:note
    # Leave the top and return so the successful retry starts a fresh wheel burst.
    _ = page.evaluate("window.__tl.vp.scrollTop = 240")
    page.wait_for_timeout(150)


def _load_older_at_top(page: Page, before_requests: list[str], paused_routes: list[Route]) -> None:
    assert _pull_history_page(page, before_requests), "the initial history page is unavailable"

    assert len(paused_routes) == 1, "the first paging request was not held"
    before_n = page.evaluate(
        "Number(window.__tl.vp.querySelector('[role=log]').dataset.timelineItemCount)"
    )

    page.evaluate("window.__tl.vp.scrollTop = 600")
    page.evaluate("window.__tl.vp.dispatchEvent(new Event('scroll'))")
    assert page.evaluate("window.__tl.vp.scrollTop") == 600
    assert (
        page.evaluate(
            "Number(window.__tl.vp.querySelector('[role=log]').dataset.timelineItemCount)"
        )
        == before_n
    ), "older history landed before the in-flight user scroll"
    paused_routes.pop().continue_()


@pytest.mark.scenario("tests.e2e.fakes.scenarios.load_older:build")
def test_load_older_preserves_reading_position(e2e_env: E2EEnv) -> None:
    page = e2e_env.page
    agent_id = e2e_env.agent_id

    before_requests: list[str] = []
    paused_routes: list[Route] = []

    def pause_first_page(route: Route) -> None:
        before_requests.append(route.request.url)
        if len(before_requests) <= 2:
            paused_routes.append(route)
        else:
            route.continue_()

    page.route(re.compile(r"/api/agents/\d+/timeline\?.*before="), pause_first_page)

    page.goto(e2e_env.agent_url)
    page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)

    page.fill('[data-testid="composer-input"]', "\u8dd1\u5b8c\u5168\u90e8\u6b65\u9aa4\u3002")
    page.click('[data-testid="composer-send"]')
    deadline = time.monotonic() + 120.0
    while True:
        items = gateway_get(
            f"{e2e_env.gateway_url}/api/agents/{agent_id}/timeline?limit=1000",
            timeout=90.0,
        ).json()["items"]
        if (
            any(
                it["kind"] == "agent_chat"
                and "\u5168\u90e8\u6267\u884c\u5b8c\u6bd5" in it["payload"]
                for it in items
            )
            or time.monotonic() > deadline
        ):
            break
        time.sleep(0.5)

    page.reload()
    page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)

    n = 0
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        n = page.evaluate(
            "Number(document.querySelector('[role=log]')?.dataset.timelineItemCount ?? 0)"
        )
        if n > 50:
            break
        page.wait_for_timeout(300)
    assert n >= 50, f"scenario timeline too short to page: {n} items"

    init = page.evaluate(_SAMPLER_JS)
    assert init.get("ok"), init

    _reject_first_page(page, before_requests, paused_routes)

    rounds = _run_rounds(
        page, before_requests, paused_routes, init["n"], agent_id, e2e_env.gateway_url
    )
    assert rounds >= 2, f"expected multiple load-older rounds, got {rounds}"
