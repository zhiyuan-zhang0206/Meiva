// meiva:note
// The fake is the port every component test reads through, so it gets the same scrutiny: each
// capability answers what the test set, records what the component asked, and never touches the
// real window.
// meiva:link ui/web/src/docs/frontend-state/browser-port.meiva.okf.md

import { describe, expect, it } from "vitest";

import { fakeBrowser } from "./testing";

describe("fakeBrowser", () => {
  it("answers the address the test set", () => {
    const browser = fakeBrowser({ href: "http://fake.local/shell/7?agent_id=7#inbox" });
    expect(browser.navigation.location()).toEqual({
      href: "http://fake.local/shell/7?agent_id=7#inbox",
      pathname: "/shell/7",
      search: "?agent_id=7",
      hash: "#inbox",
    });
    browser.moveTo("http://fake.local/control");
    expect(browser.navigation.location()?.pathname).toBe("/control");
  });

  it("keeps values in the area they were written to", () => {
    const browser = fakeBrowser({ session: { draft: "hello" } });
    expect(browser.session.read("draft")).toEqual({ state: "found", value: "hello" });
    expect(browser.local.read("draft")).toEqual({ state: "absent" });
    expect(browser.session.write("draft", "bye")).toBe("stored");
    expect(browser.sessionValues.get("draft")).toBe("bye");
  });

  it("refuses storage on request, like a private window", () => {
    const browser = fakeBrowser({ local: { "meiva.tab": "tasks" } });
    browser.refuseStorage(true);
    expect(browser.local.read("meiva.tab")).toEqual({ state: "unavailable" });
    expect(browser.local.write("meiva.tab", "inbox")).toBe("unavailable");
    expect(browser.local.remove("meiva.tab")).toBe("unavailable");
  });

  it("records the navigation the component asked for", () => {
    const browser = fakeBrowser();
    browser.navigation.push("/a", { at: 1 });
    browser.navigation.replace("/b", { at: 2 });
    browser.navigation.assign("/c");
    browser.navigation.back();
    expect(browser.actions.pushed).toEqual([{ url: "/a", state: { at: 1 } }]);
    expect(browser.actions.replaced).toEqual([{ url: "/b", state: { at: 2 } }]);
    expect(browser.actions.assigned).toEqual(["/c"]);
    expect(browser.actions.backs).toBe(1);
    expect(browser.navigation.location()?.pathname).toBe("/c");
  });

  it("delivers a traversal to whatever subscribed", () => {
    const browser = fakeBrowser({ href: "http://fake.local/a" });
    let traversed = 0;
    const stop = browser.navigation.onTraverse(() => {
      traversed += 1;
    });
    browser.traverse("http://fake.local/b");
    expect(traversed).toBe(1);
    expect(browser.navigation.location()?.pathname).toBe("/b");
    stop();
    browser.traverse("http://fake.local/c");
    expect(traversed).toBe(1);
  });

  it("answers media queries and notifies on a change", () => {
    const browser = fakeBrowser({ media: { "(pointer: coarse)": true } });
    const query = browser.media.query("(pointer: coarse)");
    expect(query.matches()).toBe(true);
    let changes = 0;
    const stop = query.subscribe(() => {
      changes += 1;
    });
    browser.answerMedia("(pointer: coarse)", false);
    expect(query.matches()).toBe(false);
    expect(changes).toBe(1);
    stop();
    browser.answerMedia("(pointer: coarse)", true);
    expect(changes).toBe(1);
  });

  it("records copies, beacons and scrolls, and can refuse them", async () => {
    const browser = fakeBrowser();
    await expect(browser.clipboard.write("hello")).resolves.toBe(true);
    browser.beacon.send("http://fake.local/t", "{}");
    browser.view.scrollIntoView({} as Element, "start");
    expect(browser.actions.copied).toEqual(["hello"]);
    expect(browser.actions.beaconed).toEqual([{ url: "http://fake.local/t", body: "{}" }]);
    expect(browser.actions.scrolled).toHaveLength(1);
    browser.clipboardAccepts(false);
    browser.beaconAccepts(false);
    await expect(browser.clipboard.write("hello")).resolves.toBe(false);
    expect(browser.beacon.send("http://fake.local/t", "{}")).toBe(false);
  });
});
