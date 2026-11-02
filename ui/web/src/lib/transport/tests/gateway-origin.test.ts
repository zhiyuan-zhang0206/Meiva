import { describe, expect, it } from "vitest";

import { gatewayBaseForPage, gatewayOriginForRequest } from "../gateway-origin";

const options = { browserOrigins: "https://console.example", gatewayPort: "20016" };
const several = {
  browserOrigins: "https://console.example, https://private.example:8443",
  gatewayPort: "20016",
};

describe("staged HTTPS browser entry", () => {
  it("keeps API, SSE and CSP on the browser-facing HTTP/2 origin", () => {
    expect(gatewayOriginForRequest(new URL("https://console.example/?agent_id=42"), options))
      .toBe("https://console.example");
  });

  it.each([
    ["http://192.0.2.2:20017/", "http://192.0.2.2:20016"],
    ["http://console.example:20017/", "http://console.example:20016"],
    ["https://other.example:8443/", "https://other.example:20016"],
    ["https://console.example:8443/", "https://console.example:20016"],
    ["http://[::1]:20017/", "http://[::1]:20016"],
  ])("preserves the direct entry %s", (input, expected) => {
    expect(gatewayOriginForRequest(new URL(input), options)).toBe(expected);
  });

  it.each([
    ["the first entry", "https://console.example/?agent_id=42", "https://console.example"],
    [
      "the second entry",
      "https://private.example:8443/control",
      "https://private.example:8443",
    ],
    ["a page outside the list", "https://other.example/", "https://other.example:20016"],
    [
      "a listed host on another port",
      "https://private.example:9443/",
      "https://private.example:20016",
    ],
    ["a direct address", "http://192.0.2.2:20017/", "http://192.0.2.2:20016"],
  ])("with several entries, %s picks the right origin", (_name, input, expected) => {
    expect(gatewayOriginForRequest(new URL(input), several)).toBe(expected);
  });

  it("treats an unset or empty entry list as no entry", () => {
    for (const browserOrigins of [undefined, "", "  "]) {
      expect(gatewayOriginForRequest(new URL("https://console.example/"), {
        browserOrigins, gatewayPort: "20016",
      })).toBe("https://console.example:20016");
    }
  });

  it("refuses an entry that is not an absolute URL instead of skipping it", () => {
    expect(() => gatewayOriginForRequest(new URL("https://console.example/"), {
      browserOrigins: "https://console.example,,https://other.example",
    })).toThrow();
  });

  it("preserves an explicit development API override", () => {
    expect(gatewayOriginForRequest(new URL("https://console.example"), {
      ...options, apiBase: "http://localhost:8001",
    })).toBe("http://localhost:8001");
  });
});

describe("the gateway base of a page", () => {
  it("is the configured API base exactly as written, whatever the page", () => {
    expect(gatewayBaseForPage({ apiBase: "http://localhost:8001/" }, new URL("https://console.example/")))
      .toBe("http://localhost:8001/");
    expect(gatewayBaseForPage({ apiBase: "http://localhost:8001" }, null)).toBe("http://localhost:8001");
  });

  it("is empty on the server, where no page exists", () => {
    expect(gatewayBaseForPage(options, null)).toBe("");
  });

  it("is the origin the request rules pick for a browser page", () => {
    expect(gatewayBaseForPage(options, new URL("https://console.example/?agent_id=42")))
      .toBe("https://console.example");
    expect(gatewayBaseForPage(options, new URL("http://192.0.2.2:20017/"))).toBe("http://192.0.2.2:20016");
  });

  it("is the page's own origin on any listed entry", () => {
    expect(gatewayBaseForPage(several, new URL("https://private.example:8443/")))
      .toBe("https://private.example:8443");
  });

  it("treats an empty API base as unset", () => {
    expect(gatewayBaseForPage({ ...options, apiBase: "" }, new URL("https://console.example/")))
      .toBe("https://console.example");
  });
});
