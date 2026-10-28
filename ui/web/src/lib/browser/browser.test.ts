import { describe, expect, it } from "vitest";

import { pageLocation } from "./browser";

describe("pageLocation", () => {
  it("splits an absolute address into the parts a caller reads", () => {
    expect(pageLocation("http://gateway.test:8000/shell/7/s1?agent_id=7&tab=tasks#inbox")).toEqual({
      href: "http://gateway.test:8000/shell/7/s1?agent_id=7&tab=tasks#inbox",
      pathname: "/shell/7/s1",
      search: "?agent_id=7&tab=tasks",
      hash: "#inbox",
    });
  });

  it("refuses a relative address instead of resolving it against an unknown base", () => {
    expect(() => pageLocation("/shell/7")).toThrow();
  });
});
