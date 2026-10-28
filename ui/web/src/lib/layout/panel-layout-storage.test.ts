// meiva:note
// The library adapter over the browser port: a saved layout reads back, and the
// two "there is no layout" answers leave the group on its defaults.

import { describe, expect, it } from "vitest";

import { fakeBrowser } from "@/lib/browser/testing";
import { panelLayoutStorage } from "./panel-layout-storage";

const KEY = "react-resizable-panels:test";
const VALUE = JSON.stringify({ "panel-sidebar": 30, "panel-main": 70 });

describe("panelLayoutStorage", () => {
  it("reads back what it wrote, through the page's own storage", () => {
    const browser = fakeBrowser({ local: {} });
    const storage = panelLayoutStorage(browser.local);

    expect(storage.getItem(KEY)).toBeNull();
    storage.setItem(KEY, VALUE);
    expect(storage.getItem(KEY)).toBe(VALUE);
    expect(browser.localValues.get(KEY)).toBe(VALUE);
  });

  it("answers no layout when this browser never saved one", () => {
    const storage = panelLayoutStorage(fakeBrowser({ local: {} }).local);
    expect(storage.getItem(KEY)).toBeNull();
  });

  it("answers no layout, and drops the write, when the frame has no storage", () => {
    const browser = fakeBrowser({ local: {} });
    browser.refuseStorage(true);
    const storage = panelLayoutStorage(browser.local);
    expect(storage.getItem(KEY)).toBeNull();
    expect(() => storage.setItem(KEY, VALUE)).not.toThrow();
    expect(browser.localValues.has(KEY)).toBe(false);
  });
});
