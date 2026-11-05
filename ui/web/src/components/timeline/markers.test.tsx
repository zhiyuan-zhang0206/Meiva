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
    expect(screen.getByTestId("marker-unrecognized")).toBeTruthy();
  });

  it("error marker renders data-testid=marker-error", () => {
    render(<EphemeralSystemMarker source={null} payload="error:redis disconnected" />);
    expect(screen.getByTestId("marker-error")).toBeTruthy();
    expect(screen.queryByTestId("marker-unrecognized")).toBeNull();
  });

  it("shows a foreground cancellation request as a status", () => {
    render(<EphemeralSystemMarker source={null} payload="status:Cancellation requested." />);
    expect(screen.getByText("Cancellation requested.")).toBeTruthy();
    expect(screen.queryByTestId("marker-error")).toBeNull();
  });

  it("filtered payloads (compact_done / compact_request / cancelled) render neither", () => {
    for (const payload of ["compact_done", "cancelled", "compact_request:x"]) {
      const { unmount } = render(<EphemeralSystemMarker source={null} payload={payload} />);
      expect(screen.queryByTestId("marker-unrecognized")).toBeNull();
      expect(screen.queryByTestId("marker-error")).toBeNull();
      unmount();
    }
  });
});

describe("classifyMarker", () => {
  it.each(NOTE_TAGS)("NoteTag %s has a card", (tag) => {
    expect(classifyMarker(tag).kind).not.toBe("ephemeral");
  });

  it("splits the vocabulary into lifecycle, memory and note", () => {
    expect(classifyMarker("lifecycle_fork")).toEqual({ kind: "lifecycle", tag: "lifecycle_fork" });
    expect(classifyMarker("agent_memory")).toEqual({ kind: "memory" });
    expect(classifyMarker("timezone")).toEqual({ kind: "note" });
  });

  it("sends an untagged or unknown source to the ephemeral path", () => {
    expect(classifyMarker(null)).toEqual({ kind: "ephemeral" });
    expect(classifyMarker("tag_from_a_newer_server")).toEqual({ kind: "ephemeral" });
  });
});

describe("isSilentMarker", () => {
  it("silences the non-human-wake reminder and nothing else in the vocabulary", () => {
    expect(NOTE_TAGS.filter((tag) => isSilentMarker(tag, "body"))).toEqual(["reply_visibility", "non_human_wake"]);
  });

  it("judges a source outside the vocabulary by its payload", () => {
    expect(isSilentMarker(null, "compact_done")).toBe(true);
    expect(isSilentMarker(null, "compact_request:abc")).toBe(true);
    expect(isSilentMarker("tag_from_a_newer_server", "cancelled")).toBe(true);
    expect(isSilentMarker(null, "error:boom")).toBe(false);
  });

  it("never silences a known note by a payload that happens to match", () => {
    expect(isSilentMarker("heartbeat", "cancelled")).toBe(false);
  });
});
