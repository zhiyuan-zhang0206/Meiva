// TaskGraph tests — the free force-directed graph + Kanban column view.
//
// Like the Graph View tests, the d3-force layout is a real, time-driven
// simulation; these tests drive it just far enough to settle card positions,
// then assert the SVG / DOM the component renders.
//
// useTasks is mocked so the view is fed a fixed task list.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { type ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TaskRow } from "@/lib/contracts/types";
import type { TasksResult } from "@/lib/notifications/use-tasks";
import { resetMockSettings } from "@/test-support/user-settings-mock";

import { TaskGraph } from "./task-graph";

// meiva:note
// The hover card reads its one row through TanStack Query, so the hover tests
// wrap their render in a client; the other tests never mount the card.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
// meiva:note
// Lower-case helper (not a component): the hover tests need the Query client the
// card's one-row read runs in; the rest never mount the card.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
const wrapGraph = (ui: ReactElement) => (
  <QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>
);

vi.mock("@/lib/state/use-user-settings", () => import("@/test-support/user-settings-mock"));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

const getTask = vi.fn<(id: number) => Promise<TaskRow>>();
vi.mock("@/lib/transport/api", async (importOriginal) => {
  const original = await importOriginal<{ api: Record<string, unknown> }>();
  return { ...original, api: { ...original.api, getTask: (id: number) => getTask(id) } };
});

const useTasks = vi.fn<(...args: string[]) => TasksResult>();
vi.mock("@/lib/notifications/use-tasks", () => ({
  useTasks: (...args: string[]) => useTasks(...args),
}));

function task(id: number, over: Partial<TaskRow> = {}): TaskRow {
  return {
    id,
    parent_id: null,
    title: `Task ${id}`,
    description: `Description for task ${id}`,
    results: null,
    status: "in_progress",
    priority: "P2",
    owner: null,
    created_by: "user",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    reminder_count: 0,
    ...over,
  };
}

function sampleTasks(): TaskRow[] {
  return [
    task(1, { title: "root", status: "in_progress" }),
    task(2, { title: "subtask-active", status: "in_progress", parent_id: 1 }),
    task(3, { title: "subtask-ip", status: "in_progress", parent_id: 1 }),
    task(4, { title: "subtask-done", status: "done", parent_id: 1 }),
    task(5, { title: "subtask-cancelled", status: "cancelled", parent_id: 1 }),
  ];
}

function ok(tasks: TaskRow[]): TasksResult {
  return { tasks, loading: false, error: false };
}

beforeEach(() => {
  queryClient.clear();
  useTasks.mockReset();
  getTask.mockReset();
  getTask.mockResolvedValue(task(0));
  // meiva:note
  // Reset persisted graph/kanban mode + toggle state (now DB-backed user
  // settings) so a previous test's mode switch or toggle doesn't leak.
  // meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
  resetMockSettings();
});

afterEach(cleanup);


function mockCardSize(width: number, height: number): () => void {
  const proto = HTMLElement.prototype;
  // Typed through a shim so the descriptor's getter returns number.
  const widthDesc = Object.getOwnPropertyDescriptor({ offsetWidth: 0 }, "offsetWidth");
  const heightDesc = Object.getOwnPropertyDescriptor({ offsetHeight: 0 }, "offsetHeight");
  const isCard = function (this: HTMLElement) {
    return this.getAttribute("role") === "tooltip";
  };
  Object.defineProperty(proto, "offsetWidth", {
    configurable: true,
    get(this: HTMLElement) {
      if (isCard.call(this)) return width;
      // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment -- DOM descriptor getters are untyped in lib.dom; narrowed immediately below
      const real = widthDesc?.get?.call(this);
      return typeof real === "number" ? real : 0;
    },
  });
  Object.defineProperty(proto, "offsetHeight", {
    configurable: true,
    get(this: HTMLElement) {
      if (isCard.call(this)) return height;
      // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment -- DOM descriptor getters are untyped in lib.dom; narrowed immediately below
      const real = heightDesc?.get?.call(this);
      return typeof real === "number" ? real : 0;
    },
  });
  return () => {
    if (widthDesc) Object.defineProperty(proto, "offsetWidth", widthDesc);
    if (heightDesc) Object.defineProperty(proto, "offsetHeight", heightDesc);
  };
}

describe("TaskGraph hover detail card", () => {
  // The instant hover card replaces the old native SVG <title> (whose
  // appearance the browser deferred — the perceived hover lag): it must show
  // the task's registry fields the moment the cursor enters the node.
  it("shows the detail card instantly on hover, with the registry fields", async () => {
    const fullTask = task(2, {
        title: "Build the widget",
        description: "A longer **description**\nspanning two lines",
        results: "Shipped in #1",
        status: "in_progress",
        priority: "P1",
        parent_id: 1,
        owner: 30,
        owner_label: "builder",
        created_by: "user",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-02T00:00:00Z",
        remind_interval_seconds: 7200,
        last_reminded_at: "2026-01-02T00:00:00Z",
        reminder_count: 3,
      });
    const tasks: TaskRow[] = [
      task(1, { title: "root", status: "in_progress" }),
      fullTask,
    ];
    useTasks.mockReturnValue(ok(tasks));
    getTask.mockResolvedValue(fullTask);
    const { container } = render(
      wrapGraph(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />),
    );

    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    const group = texts[0].closest("g")!;

    // No native <title> remains — the delayed browser tooltip is gone.
    expect(container.querySelectorAll("svg title").length).toBe(0);

    // The card appears synchronously with the mouseenter (no delay timer).
    fireEvent.mouseEnter(group);
    expect(screen.queryByRole("tooltip")).not.toBeNull();
    const card = screen.getByRole("tooltip");
    const text = card.textContent;
    expect(text).toContain("Build the widget");
    expect(text).toContain("Task #2");
    expect(text).toContain("In progress");
    expect(text).toContain("P1");
    expect(text).toContain("builder");
    expect(text).toContain("#30");
    expect(text).toContain("User"); // created_by
    expect(text).toContain("root (#1)"); // parent
    expect(text).toContain("every 2h"); // remind interval
    expect(text).toContain("3 reminders");
    // meiva:note
    // The list projection carries no text columns; the card fetches them.
    // meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
    await waitFor(() => expect(screen.getByRole("tooltip").textContent).toContain("A longer description"));
    const fetched = screen.getByRole("tooltip");
    expect(fetched.textContent).toContain("Shipped in #1");
    // Task description / results are Markdown-authored: rendered, not raw.
    expect(fetched.textContent).not.toContain("**");
    expect(card.querySelector("strong")?.textContent).toBe("description");
    expect(getTask).toHaveBeenCalledWith(2);
    expect(text).toContain("Double-click the node to open the owner");
  });

  it("says so in one line when the full row cannot be read", async () => {
    const tasks: TaskRow[] = [task(1, { title: "root" }), task(2, { title: "no-detail", parent_id: 1 })];
    useTasks.mockReturnValue(ok(tasks));
    getTask.mockRejectedValue(new Error("offline"));
    render(wrapGraph(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />));
    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    fireEvent.mouseEnter(texts[0].closest("g")!);
    await waitFor(() => expect(screen.getByTestId("task-detail-failed")).toBeTruthy());
    expect(screen.getByTestId("task-detail-failed").textContent).toContain("Could not load the full task");
    // meiva:note
    // The registry fields still render; only the text sections are missing.
    // meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
    expect(screen.getByRole("tooltip").textContent).toContain("no-detail");
  });

  it("hides the card on mouseleave (non-capped card hides immediately)", async () => {
    // A roomy canvas puts the card in the beside-node state (not height
    // capped) — the non-interactive card must hide the moment the pointer
    // leaves the node (the capped card instead gets a grace window so its
    // scroll stays reachable; covered by its own test below).
    useTasks.mockReturnValue(ok(sampleTasks()));
    const { container } = render(
      wrapGraph(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />),
    );
    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    const group = texts[0].closest("g")!;
    const canvas = container.querySelector("svg[role='img']")!.parentElement!;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 0, y: 0, width: 600, height: 400 }),
    );
    vi.spyOn(group, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 100, y: 100, width: 36, height: 36 }),
    );
    const restore = mockCardSize(200, 100);
    try {
      fireEvent.mouseEnter(group);
      expect(screen.getByRole("tooltip")).toBeTruthy();
      fireEvent.mouseLeave(group);
      expect(screen.queryByRole("tooltip")).toBeNull();
    } finally {
      restore();
    }
  });

  it("keeps the card anchored to the node while the cursor moves (no following)", async () => {
    // User ruling 2026-08-29: the detail card shows on mouseenter and hides
    // on mouseleave — it must not chase the cursor. The card is pinned to
    // the node's on-screen box; a mousemove over the node leaves it put.
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(
      wrapGraph(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />),
    );
    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    const group = texts[0].closest("g")!;

    fireEvent.mouseEnter(group);
    const card = screen.getByRole("tooltip");
    const leftAfterEnter = card.style.left;

    // A far-away mousemove must not move the card (jsdom boxes are all 0×0,
    // so the card sits at the clamped 4px anchor; cursor-following would
    // rewrite left to follow clientX).
    fireEvent.mouseMove(group, { clientX: 900, clientY: 900 });
    expect(screen.getByRole("tooltip")).toBeTruthy();
    expect(card.style.left).toBe(leftAfterEnter);
    expect(card.style.left).toBe("4px");
  });

  it("flips beside the node's LEFT edge so the card never covers the hovered node", async () => {
    // QA #990 BLOCK regression: the flip baseline must be the node's left
    // edge — the old formula anchored the flip at the right edge and parked
    // the card ON the hovered node (~70% overlap on right-edge nodes).
    useTasks.mockReturnValue(ok(sampleTasks()));
    const { container } = render(
      wrapGraph(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />),
    );
    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    const group = texts[0].closest("g")!;
    // Canvas 300×400; the node box [250, 286] × [100, 136] sits at the right
    // edge so the 200×100 card must flip.
    const canvas = container.querySelector("svg[role='img']")!.parentElement!;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 0, y: 0, width: 300, height: 400 }),
    );
    vi.spyOn(group, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 250, y: 100, width: 36, height: 36 }),
    );
    const restore = mockCardSize(200, 100);
    try {
      fireEvent.mouseEnter(group);
      const card = screen.getByRole("tooltip");
      // Default side starts at 286+14=300 and clips (300+200 > 296), so the
      // card flips: right edge at the node's left edge minus the gap —
      // left = 250 − 14 − 200 = 36. The card [36, 236] lies fully left of
      // the node [250, 286] — zero overlap. (The old formula gave 72, which
      // covered the node's right two-thirds.)
      expect(card.style.left).toBe("36px");
      expect(card.style.top).toBe("114px"); // 100 + 14, no vertical flip
      // Not height-capped: the card stays pointer-events-none so it never
      // steals the cursor from the nodes beneath it.
      expect(card.className).toContain("pointer-events-none");
    } finally {
      restore();
    }
  });

  it("places the card vertically above a mid-band node when no horizontal side fits (QA #990: 320x390)", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    const { container } = render(
      wrapGraph(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />),
    );
    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    const group = texts[0].closest("g")!;
    // 320x390 canvas; the node box [130,166] x [180,216] sits mid-band on
    // both axes. Card 200x100.
    const canvas = container.querySelector("svg[role='img']")!.parentElement!;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 0, y: 0, width: 320, height: 390 }),
    );
    vi.spyOn(group, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 130, y: 180, width: 36, height: 36 }),
    );
    const restore = mockCardSize(200, 100);
    try {
      fireEvent.mouseEnter(group);
      const card = screen.getByRole("tooltip");
      // Neither side fits (right: 166+14+200 > 316; left: 130−14−200 < 4),
      // so the card goes ABOVE the node, horizontally centered:
      // top = 180 − 14 − 100 = 66; left = center(148) − 100 = 48.
      // Card [48,248] x [66,166] sits fully above node [130,166] x [180,216]
      // — zero overlap (the old code clamped to left=4 and covered it).
      expect(card.style.left).toBe("48px");
      expect(card.style.top).toBe("66px");
    } finally {
      restore();
    }
  });

  it("caps the card height when it is taller than both vertical sides (long card, short canvas)", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    const { container } = render(
      wrapGraph(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />),
    );
    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    const group = texts[0].closest("g")!;
    const canvas = container.querySelector("svg[role='img']")!.parentElement!;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 0, y: 0, width: 320, height: 390 }),
    );
    vi.spyOn(group, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 130, y: 180, width: 36, height: 36 }),
    );
    // Card 200x300 fits nowhere beside (320 canvas) and nowhere vertically
    // (above space 162, below space 156 — both under 300).
    const restore = mockCardSize(200, 300);
    try {
      fireEvent.mouseEnter(group);
      const card = screen.getByRole("tooltip");
      // Pinned to the roomier side (above: 162px) with the height capped so
      // the card still clears the node: top = 4, bottom = 166 = node top −
      // gap. The body scrolls instead of covering the node — and the cap
      // state flips the card to pointer-events-auto so the scroll is
      // actually reachable (QA #990 delta2).
      expect(card.style.top).toBe("4px");
      expect(card.style.maxHeight).toBe("162px");
      expect(card.style.overflowY).toBe("auto");
      expect(card.className).toContain("pointer-events-auto");
    } finally {
      restore();
    }
  });

  it("never lets the hover card run off the bottom of the canvas (user report 2026-10-20)", async () => {
    // meiva:note
    // The reported shape: a tall card on a node low in the canvas. The beside
    // placement used to flip above only when the card fitted there, and
    // otherwise left it running past the bottom edge, where the canvas clipped
    // it (the legend sat right on top of that cut).
    useTasks.mockReturnValue(ok(sampleTasks()));
    const { container } = render(
      wrapGraph(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />),
    );
    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    const group = texts[0].closest("g")!;
    const canvas = container.querySelector("svg[role='img']")!.parentElement!;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 0, y: 0, width: 900, height: 900 }),
    );
    vi.spyOn(group, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 100, y: 400, width: 36, height: 36 }),
    );
    const restore = mockCardSize(320, 509);
    try {
      fireEvent.mouseEnter(group);
      const card = screen.getByRole("tooltip");
      const top = parseFloat(card.style.top);
      const cap = parseFloat(card.style.maxHeight || "0");
      expect(card.style.overflowY).toBe("auto");
      // meiva:note
      // Pinned below the node and capped to the canvas floor (900 - 4): the body
      // scrolls instead of the card being cut by the canvas edge.
      expect(top).toBe(414);
      expect(top + cap).toBeLessThanOrEqual(896);
    } finally {
      restore();
    }
  });

  it("keeps the legend readable: a card in its column stops above it", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    const { container } = render(
      wrapGraph(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />),
    );
    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    const group = texts[0].closest("g")!;
    const canvas = container.querySelector("svg[role='img']")!.parentElement!;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 0, y: 0, width: 900, height: 900 }),
    );
    vi.spyOn(group, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 600, y: 450, width: 36, height: 36 }),
    );
    const legend = container.querySelector("[class*='bottom-3'][class*='right-3']")!;
    vi.spyOn(legend, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 560, y: 700, width: 200, height: 30 }),
    );
    const restore = mockCardSize(320, 300);
    try {
      fireEvent.mouseEnter(group);
      const card = screen.getByRole("tooltip");
      // meiva:note
      // 464 + 300 would still fit the canvas (896) but would bury the legend, so
      // the card flips above the node instead.
      expect(card.style.top).toBe("136px");
      expect(parseFloat(card.style.top) + 300).toBeLessThan(700);
    } finally {
      restore();
    }
  });
});
