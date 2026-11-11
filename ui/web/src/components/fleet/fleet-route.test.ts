// The fleet page's route-id parsing — the deep links an inspector widget
// button produces (task #2909). Malformed values must be inert: a bad link
// routes to the default view, never to a wrong row.

import { describe, expect, it } from "vitest";

import { readFleetRouteIds } from "./fleet-route";

function search(qs: string): URLSearchParams {
  return new URLSearchParams(qs);
}

describe("readFleetRouteIds", () => {
  it("parses the task id", () => {
    expect(readFleetRouteIds(search("task=9"))).toEqual({ taskId: 9 });
  });

  it("an absent param is null", () => {
    expect(readFleetRouteIds(search(""))).toEqual({ taskId: null });
  });

  it("malformed and non-positive values are null, never 0 or NaN", () => {
    expect(readFleetRouteIds(search("task=-2"))).toEqual({ taskId: null });
    expect(readFleetRouteIds(search("task=abc")).taskId).toBeNull();
    expect(readFleetRouteIds(search("task=0")).taskId).toBeNull();
    expect(readFleetRouteIds(search("task=")).taskId).toBeNull();
  });
});
