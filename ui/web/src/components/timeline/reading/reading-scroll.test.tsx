import { QueryClient } from "@tanstack/react-query";
import { act, cleanup, fireEvent, waitFor } from "@testing-library/react";
import { useLayoutEffect, useRef, type ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { USER_SETTING_DEFAULTS, type BackendTimelineItem } from "@/lib/contracts/types";
import type { TimelinePositionControl } from "@/lib/timeline/reading/position-control";
import { resetTransport, transport } from "@/test-support/fake-api";
import { renderWithProviders } from "@/test-support/render";
import { timelineGeometry } from "@/test-support/timeline-geometry";
import { TimelineView } from "..";

let top = 0;
let client: QueryClient;
let geometry: ReturnType<typeof timelineGeometry>;
let imageGrowth = 0;
const resizeCallbacks = new Set<() => void>();

function MeasuredTimeline(props: ComponentProps<typeof TimelineView>) {
  const root = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => { if (root.current) geometry.prepare(root.current); });
  return <div ref={root}><TimelineView {...props} viewportObserverFactory={geometry.observer} /></div>;
}

beforeEach(() => {
  resetTransport();
  transport.getConfig.mockResolvedValue({ fields: [] });
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  client.setQueryData(["config", null], { fields: [] });
  top = 0;
  imageGrowth = 0;
  resizeCallbacks.clear();
  geometry = timelineGeometry({
    top: () => top, setTop: (value) => { top = value; },
    height: () => 5000 + imageGrowth, resizeCallbacks,
    itemTop: (id) => (Number.parseInt(id) - 1) * 300 + (Number.parseInt(id) > 3 ? imageGrowth : 0),
  });
});
afterEach(() => { cleanup(); client.clear(); resizeCallbacks.clear(); });

describe("source-qualified DOM restoration", () => {
  it.each([97, -100])("restores a folded work child at its exact offset %s", async (offset) => {
    const rows: BackendTimelineItem[] = Array.from({ length: 16 }, (_, index) => ({
      item_id: `${index + 1}.0`,
      kind: index === 4 || index === 5 ? "agent_reasoning" : "agent_chat",
      payload: `Source row ${index + 1}`, source: null, created_at: null,
      inbound_id: null, show_timestamp: false, reading_locator: `locator_${index}`,
      source_message_id: `message_${index}`, source_block_idx: 0,
    }));
    let completed = 0;
    const control: TimelinePositionControl = {
      token: {}, ready: true, writable: false,
      position: {
        contentKey: "42", scrollTop: 1500 - offset, followBottom: false,
        anchor: { itemId: "6.0", rank: 0, viewportTop: offset, readingLocator: "locator_5" },
      },
      remember: () => undefined, navigated: () => undefined,
      restored: () => { completed += 1; },
    };
    const view = renderWithProviders(<MeasuredTimeline items={rows} threadKey="42"
      scrollMemoryKey={`source-work-child-${offset}`} readingPosition={control} turnActive={false} />, {
      queryClient: client, wallNow: () => 1001, settings: { ...USER_SETTING_DEFAULTS },
    });
    const viewport = view.container.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]');
    if (!viewport) throw new Error("The timeline viewport did not mount");
    act(() => { resizeCallbacks.forEach((deliver) => deliver()); });
    await waitFor(() => expect(viewport.querySelector('.timeline-item[data-item-id="6.0"]')).toBeTruthy());
    const target = viewport.querySelector<HTMLElement>('.timeline-item[data-item-id="6.0"]');
    if (!target) throw new Error("The canonical reading child was not materialized");
    geometry.prepare(view.container);
    act(() => { resizeCallbacks.forEach((deliver) => deliver()); });
    expect(target.getBoundingClientRect().top - viewport.getBoundingClientRect().top).toBe(offset);
    expect(completed).toBeGreaterThan(0);
    fireEvent.wheel(viewport, { deltaY: 100 });
    act(() => { resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport.querySelector('.timeline-item[data-item-id="6.0"]')).toBeTruthy();
    const toggle = viewport.querySelector<HTMLButtonElement>('[data-turn-id="5.0"] > button');
    if (!toggle) throw new Error("The reading target's work disclosure did not mount");
    fireEvent.click(toggle);
    act(() => { resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport.querySelector('.timeline-item[data-item-id="6.0"]')).toBeNull();
  });

  it.each([16, 128])("materializes the final source item in %i rows before judging negative-offset range", (count) => {
    const rows: BackendTimelineItem[] = Array.from({ length: count }, (_, index) => ({
      item_id: `${index + 1}.0`, kind: "agent_chat", payload: `Primary row ${index + 1}`,
      source: null, created_at: null, inbound_id: null, show_timestamp: false,
      source_message_id: `message_${index}`, source_block_idx: 0, reading_locator: `tail_locator_${index}`,
    }));
    geometry = timelineGeometry({
      top: () => top, setTop: (value) => { top = value; }, resizeCallbacks,
      height: () => document.querySelector<HTMLElement>(`.timeline-item[data-item-id="${count}.0"]`)?.style.contentVisibility === "visible" ? count * 300 + 1400 : count * 300 + 200,
    });
    let completed = 0;
    const control: TimelinePositionControl = {
      token: {}, ready: true, writable: false,
      position: { contentKey: "42", scrollTop: (count - 1) * 300 + 100, followBottom: false,
        anchor: { itemId: `${count}.0`, rank: 0, viewportTop: -100, readingLocator: `tail_locator_${count - 1}` } },
      remember: () => undefined, navigated: () => undefined, restored: () => { completed += 1; },
    };
    const view = renderWithProviders(<MeasuredTimeline items={rows} threadKey="42"
      scrollMemoryKey={`source-intrinsic-tail-${count}`} readingPosition={control} />, { queryClient: client, wallNow: () => 1001 });
    geometry.prepare(view.container);
    act(() => { resizeCallbacks.forEach((deliver) => deliver()); });
    const viewport = view.container.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]');
    const target = viewport?.querySelector<HTMLElement>(`.timeline-item[data-item-id="${count}.0"]`);
    if (!viewport || !target) throw new Error("The final canonical reading target did not mount");
    expect(target.getBoundingClientRect().top - viewport.getBoundingClientRect().top).toBe(-100);
    expect(completed).toBeGreaterThan(0);
  });

  it.each(["load", "error"])("keeps the exact qualified item offset after a late image %s", async (event) => {
    const rows: BackendTimelineItem[] = Array.from({ length: 16 }, (_, index) => ({
      item_id: `${index + 1}.0`, kind: index === 2 ? "inbound_chat" : "agent_chat",
      payload: `Image source row ${index + 1}`, source: index === 2 ? "user" : null,
      created_at: null, inbound_id: null, show_timestamp: false,
      reading_locator: `image_locator_${index}`,
      ...(index === 2 ? { images: ["/test-reading.png"] } : {}),
    }));
    const control: TimelinePositionControl = {
      token: {}, ready: true, writable: false,
      position: { contentKey: "42", scrollTop: 1403, followBottom: false,
        anchor: { itemId: "6.0", rank: 0, viewportTop: 97, readingLocator: "image_locator_5" } },
      remember: () => undefined, navigated: () => undefined, restored: () => undefined,
    };
    const view = renderWithProviders(<MeasuredTimeline items={rows} threadKey="42"
      scrollMemoryKey={`source-late-image-${event}`} readingPosition={control} />, {
      queryClient: client, wallNow: () => 1001,
    });
    const viewport = view.container.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]');
    const target = view.container.querySelector<HTMLElement>('.timeline-item[data-item-id="6.0"]');
    if (!viewport || !target) throw new Error("The qualified image reading target did not mount");
    geometry.prepare(view.container);
    act(() => { resizeCallbacks.forEach((deliver) => deliver()); });
    expect(target.getBoundingClientRect().top - viewport.getBoundingClientRect().top).toBe(97);
    act(() => {
      imageGrowth = 240;
      fireEvent(view.getByAltText("attached image"), new Event(event));
    });
    await waitFor(() => expect(target.getBoundingClientRect().top - viewport.getBoundingClientRect().top).toBe(97));
  });
});
