// data-testid hooks on the marker render paths (Task #1018) — the stable
// selectors the panoramic e2e cases assert against:
//   marker-unrecognized — the #1017 red alarm chip (must never render)
//   marker-error       — the SSE-error [error] chip (the legit error path)
// Rendering EphemeralSystemMarker directly (no TimelineView) keeps this file
// free of the timeline mock harness; next-intl is mocked globally in
// vitest.setup.ts.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { NOTE_TAGS } from "@/lib/contracts/constants-generated";

import { EphemeralSystemMarker } from "./markers";
import { classifyMarker, isSilentMarker } from "./model/marker-classify";

// This repo's convention: explicit cleanup (no vitest globals auto-cleanup).
afterEach(() => {
  cleanup();
});

describe("marker data-testid hooks (Task #1018)", () => {
  it("unrecognized alarm renders data-testid=marker-unrecognized", () => {
    render(<EphemeralSystemMarker source={null} payload="future_kind_not_adapted" />);
    const marker = screen.getByTestId("marker-unrecognized");
    expect(marker).toBeTruthy();
    expect(marker.hasAttribute("data-quote-body")).toBe(false);
    expect(marker.querySelector("[data-quote-body]")?.tagName).toBe("PRE");
    expect(marker.querySelector("[data-quote-body]")?.textContent).toBe("future_kind_not_adapted");
    expect(screen.getByText("source = null").closest("[data-quote-body]")).toBeNull();
  });

  it("error marker renders data-testid=marker-error", () => {
    render(<EphemeralSystemMarker source={null} payload="error:redis disconnected" />);
    expect(screen.getByTestId("marker-error").hasAttribute("data-quote-body")).toBe(true);
    expect(screen.queryByTestId("marker-unrecognized")).toBeNull();
  });

  it("filtered payloads (compact_done / compact_request / cancelled / status:Cancellation requested.) render neither", () => {
    for (const payload of [
      "compact_done",
      "cancelled",
      "status:Cancellation requested.",
      "compact_request:x",
    ]) {
      const { container, unmount } = render(
        <EphemeralSystemMarker source={null} payload={payload} />,
      );
      expect(container.firstChild).toBeNull();
      expect(screen.queryByTestId("marker-unrecognized")).toBeNull();
      expect(screen.queryByTestId("marker-error")).toBeNull();
      unmount();
    }
  });
});

describe("classifyMarker", () => {
  it.each(NOTE_TAGS)("NoteTag %s has a card", (tag) => {
    expect(classifyMarker(tag).kind).not.toBe("unrecognized");
  });

  it("splits known sources and payload prefixes into typed card families", () => {
    expect(classifyMarker("lifecycle_fork")).toEqual({ kind: "lifecycle", tag: "lifecycle_fork" });
    expect(classifyMarker("agent_memory")).toEqual({ kind: "memory" });
    expect(classifyMarker("background_task", "status:done")).toEqual({ kind: "background_task" });
    expect(classifyMarker("task-notification", "status:done")).toEqual({ kind: "background_task" });
    expect(classifyMarker("timezone")).toEqual({ kind: "note" });
    expect(classifyMarker("scheduled_wake", "status:woke")).toEqual({ kind: "system_notice" });
    expect(classifyMarker("queued_input_revived", "status:revived")).toEqual({ kind: "system_notice" });
    expect(classifyMarker(null, "status:retrying")).toEqual({ kind: "system_notice" });
    expect(classifyMarker(null, "error:boom")).toEqual({ kind: "error" });
  });

  it("gives the harness switch block its own card", () => {
    expect(classifyMarker("harness_switch", "Switched harness: meiva -> codex")).toEqual({ kind: "harness_switch" });
    expect(isSilentMarker("harness_switch", "cancelled")).toBe(false);
  });

  it("sends truly unrecognized sources/payloads to the unrecognized card family", () => {
    expect(classifyMarker(null)).toEqual({ kind: "unrecognized" });
    expect(classifyMarker("tag_from_a_newer_server")).toEqual({ kind: "unrecognized" });
    expect(classifyMarker("tag_from_a_newer_server", "plain body")).toEqual({ kind: "unrecognized" });
  });
});

describe("isSilentMarker", () => {
  it("silences nothing in the NoteTag vocabulary", () => {
    expect(NOTE_TAGS.filter((tag) => isSilentMarker(tag, "body"))).toEqual([]);
  });

  it("judges a source outside the vocabulary by its payload", () => {
    expect(isSilentMarker(null, "compact_done")).toBe(true);
    expect(isSilentMarker(null, "compact_request:abc")).toBe(true);
    expect(isSilentMarker(null, "status:Cancellation requested.")).toBe(true);
    expect(isSilentMarker("tag_from_a_newer_server", "cancelled")).toBe(true);
    expect(isSilentMarker(null, "error:boom")).toBe(false);
  });

  it("preserves archived notes when the payload resembles ephemeral bookkeeping", () => {
    expect(classifyMarker("task", "cancelled")).toEqual({ kind: "note" });
    expect(isSilentMarker("task", "cancelled")).toBe(false);
  });

  it("never silences a known note by a payload that happens to match", () => {
    expect(isSilentMarker("heartbeat", "cancelled")).toBe(false);
  });
});
